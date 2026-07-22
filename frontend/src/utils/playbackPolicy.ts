export interface PlaybackPolicy {
  targetQueueSeconds: number;
  urgentQueueSeconds: number;
  limitQueueSeconds: number;
  catchUpReleaseSeconds: number;
  urgentReleaseSeconds: number;
  normalRate: number;
  catchUpRate: number;
  urgentRate: number;
}

export type PlaybackMode = 'normal' | 'catch-up' | 'urgent' | 'over-limit';

export const DEFAULT_PLAYBACK_POLICY: PlaybackPolicy = {
  targetQueueSeconds: 5,
  urgentQueueSeconds: 8,
  limitQueueSeconds: 10,
  catchUpReleaseSeconds: 4,
  urgentReleaseSeconds: 7,
  normalRate: 1,
  catchUpRate: 1.05,
  urgentRate: 1.1,
};

export interface PlaybackQueueSample {
  timestampSeconds: number;
  queueDepthSeconds: number;
  playbackRate: number;
}

export interface PlaybackQueueSummary {
  sampleCount: number;
  p50QueueDepthSeconds: number;
  p95QueueDepthSeconds: number;
  maxQueueDepthSeconds: number;
  secondsAboveTarget: number;
  secondsAboveLimit: number;
  percentTimeAboveTarget: number;
  percentTimeAboveLimit: number;
  maxPlaybackRate: number;
}

export function selectPlaybackRate(
  projectedQueueSeconds: number,
  policy: PlaybackPolicy = DEFAULT_PLAYBACK_POLICY,
): number {
  if (projectedQueueSeconds >= policy.urgentQueueSeconds) {
    return policy.urgentRate;
  }
  if (projectedQueueSeconds >= policy.targetQueueSeconds) {
    return policy.catchUpRate;
  }
  return policy.normalRate;
}

export function selectPlaybackMode(
  projectedQueueSeconds: number,
  currentMode: PlaybackMode = 'normal',
  policy: PlaybackPolicy = DEFAULT_PLAYBACK_POLICY,
): PlaybackMode {
  if (projectedQueueSeconds > policy.limitQueueSeconds) {
    return 'over-limit';
  }

  if (currentMode === 'urgent' || currentMode === 'over-limit') {
    if (projectedQueueSeconds >= policy.urgentReleaseSeconds) return 'urgent';
    if (projectedQueueSeconds >= policy.catchUpReleaseSeconds) return 'catch-up';
    return 'normal';
  }

  if (currentMode === 'catch-up') {
    if (projectedQueueSeconds >= policy.urgentQueueSeconds) return 'urgent';
    if (projectedQueueSeconds >= policy.catchUpReleaseSeconds) return 'catch-up';
    return 'normal';
  }

  if (projectedQueueSeconds >= policy.urgentQueueSeconds) return 'urgent';
  if (projectedQueueSeconds >= policy.targetQueueSeconds) return 'catch-up';
  return 'normal';
}

export function playbackRateForMode(
  mode: PlaybackMode,
  policy: PlaybackPolicy = DEFAULT_PLAYBACK_POLICY,
): number {
  if (mode === 'over-limit' || mode === 'urgent') return policy.urgentRate;
  if (mode === 'catch-up') return policy.catchUpRate;
  return policy.normalRate;
}

function percentile(values: number[], quantile: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.max(0, Math.ceil(quantile * sorted.length) - 1);
  return sorted[index];
}

export function summarizePlaybackQueue(
  samples: PlaybackQueueSample[],
  policy: PlaybackPolicy = DEFAULT_PLAYBACK_POLICY,
): PlaybackQueueSummary {
  if (samples.length === 0) {
    return {
      sampleCount: 0,
      p50QueueDepthSeconds: 0,
      p95QueueDepthSeconds: 0,
      maxQueueDepthSeconds: 0,
      secondsAboveTarget: 0,
      secondsAboveLimit: 0,
      percentTimeAboveTarget: 0,
      percentTimeAboveLimit: 0,
      maxPlaybackRate: policy.normalRate,
    };
  }

  const ordered = [...samples].sort(
    (a, b) => a.timestampSeconds - b.timestampSeconds,
  );
  let secondsAboveTarget = 0;
  let secondsAboveLimit = 0;

  for (let i = 0; i < ordered.length - 1; i += 1) {
    const interval = Math.max(
      0,
      ordered[i + 1].timestampSeconds - ordered[i].timestampSeconds,
    );
    if (ordered[i].queueDepthSeconds > policy.targetQueueSeconds) {
      secondsAboveTarget += interval;
    }
    if (ordered[i].queueDepthSeconds > policy.limitQueueSeconds) {
      secondsAboveLimit += interval;
    }
  }

  const observedDuration = Math.max(
    0,
    ordered[ordered.length - 1].timestampSeconds - ordered[0].timestampSeconds,
  );
  const queueDepths = ordered.map((sample) => sample.queueDepthSeconds);

  return {
    sampleCount: ordered.length,
    p50QueueDepthSeconds: percentile(queueDepths, 0.5),
    p95QueueDepthSeconds: percentile(queueDepths, 0.95),
    maxQueueDepthSeconds: Math.max(...queueDepths),
    secondsAboveTarget,
    secondsAboveLimit,
    percentTimeAboveTarget:
      observedDuration > 0 ? (secondsAboveTarget / observedDuration) * 100 : 0,
    percentTimeAboveLimit:
      observedDuration > 0 ? (secondsAboveLimit / observedDuration) * 100 : 0,
    maxPlaybackRate: Math.max(...ordered.map((sample) => sample.playbackRate)),
  };
}
