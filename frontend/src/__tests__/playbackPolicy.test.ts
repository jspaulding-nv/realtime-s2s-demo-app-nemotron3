import { describe, expect, it } from 'vitest';
import {
  DEFAULT_PLAYBACK_POLICY,
  playbackRateForMode,
  selectPlaybackMode,
  selectPlaybackRate,
  summarizePlaybackQueue,
} from '../utils/playbackPolicy';

describe('playbackPolicy', () => {
  it('selects the configured rate tiers at their boundaries', () => {
    expect(selectPlaybackRate(4.99)).toBe(1);
    expect(selectPlaybackRate(5)).toBe(1.05);
    expect(selectPlaybackRate(8)).toBe(1.1);
    expect(selectPlaybackRate(12)).toBe(1.1);
  });

  it('uses hysteresis when releasing catch-up and urgent modes', () => {
    expect(selectPlaybackMode(5, 'normal')).toBe('catch-up');
    expect(selectPlaybackMode(4.5, 'catch-up')).toBe('catch-up');
    expect(selectPlaybackMode(3.99, 'catch-up')).toBe('normal');

    expect(selectPlaybackMode(8, 'catch-up')).toBe('urgent');
    expect(selectPlaybackMode(7.5, 'urgent')).toBe('urgent');
    expect(selectPlaybackMode(6.99, 'urgent')).toBe('catch-up');
  });

  it('marks an over-limit queue while preserving the urgent rate', () => {
    const mode = selectPlaybackMode(10.01, 'urgent');
    expect(mode).toBe('over-limit');
    expect(playbackRateForMode(mode)).toBe(1.1);
    expect(selectPlaybackMode(9, mode)).toBe('urgent');
  });

  it('summarizes queue percentiles and time above SLA thresholds', () => {
    const summary = summarizePlaybackQueue([
      { timestampSeconds: 0, queueDepthSeconds: 2, playbackRate: 1 },
      { timestampSeconds: 2, queueDepthSeconds: 6, playbackRate: 1.05 },
      { timestampSeconds: 5, queueDepthSeconds: 12, playbackRate: 1.1 },
      { timestampSeconds: 9, queueDepthSeconds: 3, playbackRate: 1 },
    ]);

    expect(summary.p50QueueDepthSeconds).toBe(3);
    expect(summary.p95QueueDepthSeconds).toBe(12);
    expect(summary.maxQueueDepthSeconds).toBe(12);
    expect(summary.secondsAboveTarget).toBe(7);
    expect(summary.secondsAboveLimit).toBe(4);
    expect(summary.percentTimeAboveTarget).toBeCloseTo((7 / 9) * 100);
    expect(summary.maxPlaybackRate).toBe(1.1);
  });

  it('returns a zero summary for no samples', () => {
    expect(summarizePlaybackQueue([])).toEqual({
      sampleCount: 0,
      p50QueueDepthSeconds: 0,
      p95QueueDepthSeconds: 0,
      maxQueueDepthSeconds: 0,
      secondsAboveTarget: 0,
      secondsAboveLimit: 0,
      percentTimeAboveTarget: 0,
      percentTimeAboveLimit: 0,
      maxPlaybackRate: DEFAULT_PLAYBACK_POLICY.normalRate,
    });
  });
});
