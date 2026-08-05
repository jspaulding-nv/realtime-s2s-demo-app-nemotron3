import type {
  AudioFrameObservation,
  AudioParentCompleteObservation,
} from '../types/audioMetadata';
import {
  DEFAULT_PLAYBACK_POLICY,
  playbackRateForMode,
  selectPlaybackMode,
  type PlaybackMode,
  type PlaybackPolicy,
} from './playbackPolicy';

const TIME_EPSILON_SECONDS = 1e-9;

export const TAIL_FRESHNESS_SHADOW_SCHEMA = (
  'tail-freshness-shadow/v1'
) as const;
export const TAIL_FRESHNESS_SHADOW_STRATEGY = (
  'truncate_parent_tail'
) as const;

export interface TailFreshnessShadowConfig {
  hardCapSeconds?: number;
  cancellationGuardSeconds?: number;
  adaptivePlayback?: boolean;
  playbackPolicy?: PlaybackPolicy;
}

export interface TailFreshnessShadowFrameInput {
  observation: AudioFrameObservation;
  schedulePerformanceMs: number;
  audioContextTimeAtScheduleSeconds: number;
  audioBytes: number;
  sourceDurationSeconds: number;
}

export interface TailFreshnessShadowDecision {
  streamGeneration: number;
  parentSequenceId: number;
  audioFrameId: number;
  schedulePerformanceMs: number;
  audioContextTimeAtScheduleSeconds: number;
  audioBytes: number;
  sourceDurationSeconds: number;
  queueBeforeTruncationSeconds: number;
  queueAfterTruncationSeconds: number;
  peakQueueAfterTruncationSeconds: number;
  playbackRate: number;
  playbackMode: PlaybackMode;
  truncationTriggered: boolean;
  suppressedArrival: boolean;
  droppedFrameCountThisEvent: number;
  droppedSourceDurationThisEventSeconds: number;
  residualOverCapSeconds: number;
}

export type TailFreshnessParentShape =
  | 'untouched'
  | 'fully_dropped'
  | 'partial_suffix';

export interface TailFreshnessShadowParentResult {
  parentSequenceId: number;
  frameCount: number;
  retainedFrameCount: number;
  droppedFrameCount: number;
  sourceDurationSeconds: number;
  retainedSourceDurationSeconds: number;
  droppedSourceDurationSeconds: number;
  firstDroppedFrameId: number | null;
  shape: TailFreshnessParentShape;
}

export interface TailFreshnessShadowSummary {
  framesReceived: number;
  framesRetained: number;
  framesDropped: number;
  parentsReceived: number;
  parentsTruncated: number;
  parentsFullyDropped: number;
  totalSourceDurationSeconds: number;
  retainedSourceDurationSeconds: number;
  droppedSourceDurationSeconds: number;
  retainedSourcePercent: number;
  lastDecisionQueueSeconds: number;
  peakQueueBeforeTruncationSeconds: number;
  peakQueueAfterTruncationSeconds: number;
  truncationTriggerCount: number;
  suppressedArrivalCount: number;
  residualBreachEvents: number;
  peakResidualOverCapSeconds: number;
  maxDroppedParentSuffixSeconds: number;
  hardCapAchieved: boolean;
  singleTailContractHolds: boolean;
}

export interface TailFreshnessShadowEvidence {
  schema: typeof TAIL_FRESHNESS_SHADOW_SCHEMA;
  strategy: typeof TAIL_FRESHNESS_SHADOW_STRATEGY;
  status: 'complete';
  evidenceValid: true;
  observationOnly: true;
  liveAudioChanged: false;
  containsPcm: false;
  containsTranscriptOrTranslationText: false;
  hardCapSeconds: number;
  cancellationGuardSeconds: number;
  adaptivePlayback: boolean;
  playbackPolicy: PlaybackPolicy;
  summary: TailFreshnessShadowSummary;
  parents: TailFreshnessShadowParentResult[];
  decisions: TailFreshnessShadowDecision[];
}

export function serializeTailFreshnessShadowEvidence(
  evidence: TailFreshnessShadowEvidence,
): string {
  return `${JSON.stringify(evidence, null, 2)}\n`;
}

interface ShadowFrameState {
  streamGeneration: number;
  parentSequenceId: number;
  audioFrameId: number;
  sourceDurationSeconds: number;
  startSeconds: number;
  endSeconds: number;
  playbackRate: number;
  playbackMode: PlaybackMode;
}

interface ParentAccumulator {
  parentSequenceId: number;
  frameCount: number;
  audioBytes: number;
  sourceDurationSeconds: number;
}

export class TailFreshnessShadowError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'TailFreshnessShadowError';
  }
}

function requireFiniteNonnegative(value: number, field: string): void {
  if (!Number.isFinite(value) || value < 0) {
    throw new TailFreshnessShadowError(
      `${field} must be finite and non-negative`,
    );
  }
}

function requireFinitePositive(value: number, field: string): void {
  if (!Number.isFinite(value) || value <= 0) {
    throw new TailFreshnessShadowError(
      `${field} must be finite and positive`,
    );
  }
}

function copyPlaybackPolicy(policy: PlaybackPolicy): PlaybackPolicy {
  return { ...policy };
}

function validatePlaybackPolicy(policy: PlaybackPolicy): void {
  const entries = Object.entries(policy);
  for (const [field, value] of entries) {
    requireFinitePositive(value, `playbackPolicy.${field}`);
  }
  if (
    !(policy.catchUpReleaseSeconds < policy.targetQueueSeconds)
    || !(policy.targetQueueSeconds <= policy.urgentReleaseSeconds)
    || !(policy.urgentReleaseSeconds < policy.urgentQueueSeconds)
    || !(policy.urgentQueueSeconds <= policy.limitQueueSeconds)
    || !(policy.normalRate <= policy.catchUpRate)
    || !(policy.catchUpRate <= policy.urgentRate)
  ) {
    throw new TailFreshnessShadowError('playbackPolicy ordering is invalid');
  }
}

function queueDepth(
  states: readonly ShadowFrameState[],
  atSeconds: number,
): number {
  if (states.length === 0) return 0;
  return Math.max(0, states[states.length - 1].endSeconds - atSeconds);
}

export class TailFreshnessShadowScheduler {
  private readonly hardCapSeconds: number;
  private readonly cancellationGuardSeconds: number;
  private readonly adaptivePlayback: boolean;
  private readonly playbackPolicy: PlaybackPolicy;

  private active = false;
  private failed = false;
  private finished = false;
  private streamGeneration: number | null = null;
  private lastContextSeconds: number | null = null;
  private expectedParentSequenceId = 0;
  private currentParent: ParentAccumulator | null = null;
  private readonly completedParentIds = new Set<number>();
  private readonly suppressedParentIds = new Set<number>();
  private readonly states: ShadowFrameState[] = [];
  private readonly droppedStates: ShadowFrameState[] = [];
  private readonly decisions: TailFreshnessShadowDecision[] = [];
  private readonly parentTotals = new Map<number, ParentAccumulator>();
  private peakQueueBeforeTruncationSeconds = 0;
  private peakQueueAfterTruncationSeconds = 0;
  private truncationTriggerCount = 0;
  private suppressedArrivalCount = 0;
  private residualBreachEvents = 0;
  private peakResidualOverCapSeconds = 0;

  constructor(config: TailFreshnessShadowConfig = {}) {
    this.hardCapSeconds = config.hardCapSeconds ?? 10;
    this.cancellationGuardSeconds = (
      config.cancellationGuardSeconds ?? 0.1
    );
    this.adaptivePlayback = config.adaptivePlayback ?? true;
    this.playbackPolicy = copyPlaybackPolicy(
      config.playbackPolicy ?? DEFAULT_PLAYBACK_POLICY,
    );
    requireFinitePositive(this.hardCapSeconds, 'hardCapSeconds');
    requireFiniteNonnegative(
      this.cancellationGuardSeconds,
      'cancellationGuardSeconds',
    );
    if (typeof this.adaptivePlayback !== 'boolean') {
      throw new TailFreshnessShadowError(
        'adaptivePlayback must be a boolean',
      );
    }
    validatePlaybackPolicy(this.playbackPolicy);
  }

  begin(): void {
    if (this.active || this.finished) {
      throw new TailFreshnessShadowError(
        'shadow scheduler instances are single-use',
      );
    }
    this.active = true;
  }

  observeFrame(
    input: TailFreshnessShadowFrameInput,
  ): TailFreshnessShadowDecision {
    this.ensureUsable();
    const metadata = input.observation.metadata;
    const atSeconds = input.audioContextTimeAtScheduleSeconds;
    requireFiniteNonnegative(atSeconds, 'audioContextTimeAtScheduleSeconds');
    requireFiniteNonnegative(
      input.schedulePerformanceMs,
      'schedulePerformanceMs',
    );
    requireFinitePositive(input.sourceDurationSeconds, 'sourceDurationSeconds');
    if (
      !Number.isSafeInteger(input.audioBytes)
      || input.audioBytes <= 0
      || input.audioBytes !== metadata.audioBytes
    ) {
      return this.raise('scheduled audioBytes do not match frame metadata');
    }
    const metadataDuration = (
      metadata.audioBytes
      / metadata.channels
      / metadata.bytesPerSample
      / metadata.sampleRateHz
    );
    if (
      Math.abs(metadataDuration - input.sourceDurationSeconds)
      > 1 / metadata.sampleRateHz
    ) {
      return this.raise(
        'scheduled source duration does not match frame metadata',
      );
    }
    if (
      this.lastContextSeconds !== null
      && atSeconds + TIME_EPSILON_SECONDS < this.lastContextSeconds
    ) {
      return this.raise('AudioContext schedule time moved backward');
    }
    this.lastContextSeconds = atSeconds;
    this.acceptFrameIdentity(metadata.streamGeneration, metadata.parentSequenceId,
      metadata.audioFrameId, metadata.audioBytes, input.sourceDurationSeconds);

    const previousMode: PlaybackMode = this.states.length > 0
      ? this.states[this.states.length - 1].playbackMode
      : 'normal';
    const startSeconds = Math.max(
      this.states.length > 0
        ? this.states[this.states.length - 1].endSeconds
        : 0,
      atSeconds,
    );
    const projectedQueueAtNormalRateSeconds = (
      startSeconds - atSeconds + input.sourceDurationSeconds
    );
    const playbackMode = this.adaptivePlayback
      ? selectPlaybackMode(
          projectedQueueAtNormalRateSeconds,
          previousMode,
          this.playbackPolicy,
        )
      : 'normal';
    const playbackRate = playbackRateForMode(
      playbackMode,
      this.playbackPolicy,
    );
    const appended: ShadowFrameState = {
      streamGeneration: metadata.streamGeneration,
      parentSequenceId: metadata.parentSequenceId,
      audioFrameId: metadata.audioFrameId,
      sourceDurationSeconds: input.sourceDurationSeconds,
      startSeconds,
      endSeconds: startSeconds + input.sourceDurationSeconds / playbackRate,
      playbackRate,
      playbackMode,
    };
    this.states.push(appended);
    const queueBeforeTruncationSeconds = queueDepth(this.states, atSeconds);
    this.peakQueueBeforeTruncationSeconds = Math.max(
      this.peakQueueBeforeTruncationSeconds,
      queueBeforeTruncationSeconds,
    );

    const suppressedArrival = this.suppressedParentIds.has(
      metadata.parentSequenceId,
    );
    let truncationTriggered = false;
    const droppedThisEvent: ShadowFrameState[] = [];

    if (suppressedArrival) {
      const removed = this.states.pop();
      if (removed !== appended) {
        return this.raise('suppressed frame was not the newest shadow state');
      }
      droppedThisEvent.push(appended);
      this.suppressedArrivalCount += 1;
    } else if (
      queueBeforeTruncationSeconds
      > this.hardCapSeconds + TIME_EPSILON_SECONDS
    ) {
      truncationTriggered = true;
      this.truncationTriggerCount += 1;
      this.truncateUntilBounded(atSeconds, droppedThisEvent);
    }

    this.droppedStates.push(...droppedThisEvent);
    const queueAfterTruncationSeconds = queueDepth(this.states, atSeconds);
    this.peakQueueAfterTruncationSeconds = Math.max(
      this.peakQueueAfterTruncationSeconds,
      queueAfterTruncationSeconds,
    );
    const residualOverCapSeconds = Math.max(
      0,
      queueAfterTruncationSeconds - this.hardCapSeconds,
    );
    if (residualOverCapSeconds > TIME_EPSILON_SECONDS) {
      this.residualBreachEvents += 1;
      this.peakResidualOverCapSeconds = Math.max(
        this.peakResidualOverCapSeconds,
        residualOverCapSeconds,
      );
    }

    const decision: TailFreshnessShadowDecision = {
      streamGeneration: metadata.streamGeneration,
      parentSequenceId: metadata.parentSequenceId,
      audioFrameId: metadata.audioFrameId,
      schedulePerformanceMs: input.schedulePerformanceMs,
      audioContextTimeAtScheduleSeconds: atSeconds,
      audioBytes: input.audioBytes,
      sourceDurationSeconds: input.sourceDurationSeconds,
      queueBeforeTruncationSeconds,
      queueAfterTruncationSeconds,
      peakQueueAfterTruncationSeconds: this.peakQueueAfterTruncationSeconds,
      playbackRate,
      playbackMode,
      truncationTriggered,
      suppressedArrival,
      droppedFrameCountThisEvent: droppedThisEvent.length,
      droppedSourceDurationThisEventSeconds: droppedThisEvent.reduce(
        (total, state) => total + state.sourceDurationSeconds,
        0,
      ),
      residualOverCapSeconds,
    };
    this.decisions.push(decision);
    return { ...decision };
  }

  observeParentComplete(
    observation: AudioParentCompleteObservation,
  ): void {
    this.ensureUsable();
    const metadata = observation.metadata;
    if (!this.currentParent) {
      return this.raise('parent completion arrived without an active parent');
    }
    if (
      metadata.streamGeneration !== this.streamGeneration
      || metadata.parentSequenceId !== this.currentParent.parentSequenceId
      || metadata.audioFrameCount !== this.currentParent.frameCount
      || metadata.audioBytes !== this.currentParent.audioBytes
    ) {
      return this.raise('parent completion does not match observed frames');
    }
    this.completedParentIds.add(metadata.parentSequenceId);
    this.suppressedParentIds.delete(metadata.parentSequenceId);
    this.parentTotals.set(metadata.parentSequenceId, {
      ...this.currentParent,
    });
    this.currentParent = null;
    this.expectedParentSequenceId += 1;
  }

  getSummary(): TailFreshnessShadowSummary {
    this.ensureReadable();
    return this.buildSummary(this.lastContextSeconds ?? 0);
  }

  finish(): TailFreshnessShadowEvidence {
    this.ensureUsable();
    if (this.currentParent !== null) {
      return this.raise('stream ended before the active parent completed');
    }
    if (this.suppressedParentIds.size !== 0) {
      return this.raise('stream ended with a suppressed parent still active');
    }
    if (this.decisions.length === 0) {
      return this.raise('stream ended without any translated audio frames');
    }
    this.active = false;
    this.finished = true;
    const parents = this.buildParentResults();
    const summary = this.buildSummary(this.lastContextSeconds ?? 0, parents);
    if (!summary.singleTailContractHolds) {
      this.failed = true;
      throw new TailFreshnessShadowError(
        'single-tail loss-shape invariant failed',
      );
    }
    return {
      schema: TAIL_FRESHNESS_SHADOW_SCHEMA,
      strategy: TAIL_FRESHNESS_SHADOW_STRATEGY,
      status: 'complete',
      evidenceValid: true,
      observationOnly: true,
      liveAudioChanged: false,
      containsPcm: false,
      containsTranscriptOrTranslationText: false,
      hardCapSeconds: this.hardCapSeconds,
      cancellationGuardSeconds: this.cancellationGuardSeconds,
      adaptivePlayback: this.adaptivePlayback,
      playbackPolicy: copyPlaybackPolicy(this.playbackPolicy),
      summary,
      parents,
      decisions: this.decisions.map((decision) => ({ ...decision })),
    };
  }

  private truncateUntilBounded(
    atSeconds: number,
    droppedThisEvent: ShadowFrameState[],
  ): void {
    const protectedThrough = (
      atSeconds + this.cancellationGuardSeconds + TIME_EPSILON_SECONDS
    );
    while (
      queueDepth(this.states, atSeconds)
      > this.hardCapSeconds + TIME_EPSILON_SECONDS
    ) {
      const victim = this.states.find(
        (state) => state.startSeconds > protectedThrough,
      );
      if (!victim) return;
      const eligible = this.states.filter(
        (state) => (
          state.parentSequenceId === victim.parentSequenceId
          && state.startSeconds > protectedThrough
        ),
      );
      if (eligible.length === 0) return;

      const removed: ShadowFrameState[] = [];
      for (let index = eligible.length - 1; index >= 0; index -= 1) {
        const suffixState = eligible[index];
        const stateIndex = this.states.indexOf(suffixState);
        if (stateIndex < 0) {
          this.raise('eligible suffix frame disappeared during truncation');
        }
        this.states.splice(stateIndex, 1);
        removed.push(suffixState);
        this.rescheduleFuture(atSeconds);
        if (
          queueDepth(this.states, atSeconds)
          <= this.hardCapSeconds + TIME_EPSILON_SECONDS
        ) {
          break;
        }
      }
      droppedThisEvent.push(...removed.reverse());
      if (!this.completedParentIds.has(victim.parentSequenceId)) {
        this.suppressedParentIds.add(victim.parentSequenceId);
      }
    }
  }

  private rescheduleFuture(atSeconds: number): void {
    let protectedCount = 0;
    for (const state of this.states) {
      if (state.startSeconds <= atSeconds + TIME_EPSILON_SECONDS) {
        protectedCount += 1;
      } else {
        break;
      }
    }
    const protectedEnd = protectedCount > 0
      ? this.states[protectedCount - 1].endSeconds
      : atSeconds;
    let nextStartSeconds = Math.max(atSeconds, protectedEnd);
    for (let index = protectedCount; index < this.states.length; index += 1) {
      const state = this.states[index];
      state.startSeconds = nextStartSeconds;
      state.endSeconds = (
        nextStartSeconds + state.sourceDurationSeconds / state.playbackRate
      );
      nextStartSeconds = state.endSeconds;
    }
  }

  private acceptFrameIdentity(
    streamGeneration: number,
    parentSequenceId: number,
    audioFrameId: number,
    audioBytes: number,
    sourceDurationSeconds: number,
  ): void {
    if (this.streamGeneration === null) {
      this.streamGeneration = streamGeneration;
    } else if (streamGeneration !== this.streamGeneration) {
      this.raise('stream generation changed inside the shadow window');
    }
    if (this.currentParent === null) {
      if (parentSequenceId !== this.expectedParentSequenceId) {
        this.raise('parent sequence is not contiguous and zero-based');
      }
      if (audioFrameId !== 0) {
        this.raise('the first parent frame must have audioFrameId zero');
      }
      this.currentParent = {
        parentSequenceId,
        frameCount: 0,
        audioBytes: 0,
        sourceDurationSeconds: 0,
      };
    } else if (parentSequenceId !== this.currentParent.parentSequenceId) {
      this.raise('a parent must complete before the next parent begins');
    }
    if (audioFrameId !== this.currentParent.frameCount) {
      this.raise('audio frame sequence is not contiguous and zero-based');
    }
    this.currentParent.frameCount += 1;
    this.currentParent.audioBytes += audioBytes;
    this.currentParent.sourceDurationSeconds += sourceDurationSeconds;
  }

  private buildParentResults(): TailFreshnessShadowParentResult[] {
    const retainedByParent = new Map<number, ShadowFrameState[]>();
    const droppedByParent = new Map<number, ShadowFrameState[]>();
    for (const state of this.states) {
      const items = retainedByParent.get(state.parentSequenceId) ?? [];
      items.push(state);
      retainedByParent.set(state.parentSequenceId, items);
    }
    for (const state of this.droppedStates) {
      const items = droppedByParent.get(state.parentSequenceId) ?? [];
      items.push(state);
      droppedByParent.set(state.parentSequenceId, items);
    }

    const results: TailFreshnessShadowParentResult[] = [];
    for (const [parentSequenceId, total] of this.parentTotals) {
      const retained = retainedByParent.get(parentSequenceId) ?? [];
      const dropped = droppedByParent.get(parentSequenceId) ?? [];
      const droppedIds = dropped
        .map((state) => state.audioFrameId)
        .sort((left, right) => left - right);
      const firstDroppedFrameId = droppedIds.length > 0
        ? droppedIds[0]
        : null;
      const expectedSuffix = firstDroppedFrameId === null
        ? []
        : Array.from(
            { length: total.frameCount - firstDroppedFrameId },
            (_, index) => firstDroppedFrameId + index,
          );
      if (
        droppedIds.length > 0
        && (
          droppedIds.length !== expectedSuffix.length
          || droppedIds.some((value, index) => value !== expectedSuffix[index])
        )
      ) {
        this.failed = true;
        throw new TailFreshnessShadowError(
          `parentSequenceId ${parentSequenceId} has non-suffix loss`,
        );
      }
      const retainedSourceDurationSeconds = retained.reduce(
        (sum, state) => sum + state.sourceDurationSeconds,
        0,
      );
      const droppedSourceDurationSeconds = dropped.reduce(
        (sum, state) => sum + state.sourceDurationSeconds,
        0,
      );
      results.push({
        parentSequenceId,
        frameCount: total.frameCount,
        retainedFrameCount: retained.length,
        droppedFrameCount: dropped.length,
        sourceDurationSeconds: total.sourceDurationSeconds,
        retainedSourceDurationSeconds,
        droppedSourceDurationSeconds,
        firstDroppedFrameId,
        shape: dropped.length === 0
          ? 'untouched'
          : retained.length === 0
            ? 'fully_dropped'
            : 'partial_suffix',
      });
    }
    return results;
  }

  private buildSummary(
    atSeconds: number,
    suppliedParents?: TailFreshnessShadowParentResult[],
  ): TailFreshnessShadowSummary {
    const parents = suppliedParents ?? this.buildReadableParentResults();
    const totalSourceDurationSeconds = parents.reduce(
      (sum, parent) => sum + parent.sourceDurationSeconds,
      0,
    );
    const retainedSourceDurationSeconds = this.states.reduce(
      (sum, state) => sum + state.sourceDurationSeconds,
      0,
    );
    const droppedSourceDurationSeconds = this.droppedStates.reduce(
      (sum, state) => sum + state.sourceDurationSeconds,
      0,
    );
    return {
      framesReceived: this.states.length + this.droppedStates.length,
      framesRetained: this.states.length,
      framesDropped: this.droppedStates.length,
      parentsReceived: parents.length,
      parentsTruncated: parents.filter(
        (parent) => parent.shape === 'partial_suffix',
      ).length,
      parentsFullyDropped: parents.filter(
        (parent) => parent.shape === 'fully_dropped',
      ).length,
      totalSourceDurationSeconds,
      retainedSourceDurationSeconds,
      droppedSourceDurationSeconds,
      retainedSourcePercent: totalSourceDurationSeconds > 0
        ? retainedSourceDurationSeconds / totalSourceDurationSeconds * 100
        : 0,
      lastDecisionQueueSeconds: queueDepth(this.states, atSeconds),
      peakQueueBeforeTruncationSeconds: (
        this.peakQueueBeforeTruncationSeconds
      ),
      peakQueueAfterTruncationSeconds: this.peakQueueAfterTruncationSeconds,
      truncationTriggerCount: this.truncationTriggerCount,
      suppressedArrivalCount: this.suppressedArrivalCount,
      residualBreachEvents: this.residualBreachEvents,
      peakResidualOverCapSeconds: this.peakResidualOverCapSeconds,
      maxDroppedParentSuffixSeconds: parents.reduce(
        (maximum, parent) => Math.max(
          maximum,
          parent.droppedSourceDurationSeconds,
        ),
        0,
      ),
      hardCapAchieved: this.residualBreachEvents === 0,
      singleTailContractHolds: parents.every(
        (parent) => (
          parent.shape === 'untouched'
          || parent.shape === 'fully_dropped'
          || parent.shape === 'partial_suffix'
        ),
      ),
    };
  }

  private buildReadableParentResults(): TailFreshnessShadowParentResult[] {
    const completed = this.buildParentResults();
    if (!this.currentParent) return completed;
    const retained = this.states.filter(
      (state) => state.parentSequenceId === this.currentParent?.parentSequenceId,
    );
    const dropped = this.droppedStates.filter(
      (state) => state.parentSequenceId === this.currentParent?.parentSequenceId,
    );
    const droppedSourceDurationSeconds = dropped.reduce(
      (sum, state) => sum + state.sourceDurationSeconds,
      0,
    );
    completed.push({
      parentSequenceId: this.currentParent.parentSequenceId,
      frameCount: this.currentParent.frameCount,
      retainedFrameCount: retained.length,
      droppedFrameCount: dropped.length,
      sourceDurationSeconds: this.currentParent.sourceDurationSeconds,
      retainedSourceDurationSeconds: retained.reduce(
        (sum, state) => sum + state.sourceDurationSeconds,
        0,
      ),
      droppedSourceDurationSeconds,
      firstDroppedFrameId: dropped.length > 0
        ? Math.min(...dropped.map((state) => state.audioFrameId))
        : null,
      shape: dropped.length === 0
        ? 'untouched'
        : retained.length === 0
          ? 'fully_dropped'
          : 'partial_suffix',
    });
    return completed;
  }

  private ensureUsable(): void {
    if (this.failed) {
      throw new TailFreshnessShadowError(
        'shadow scheduler is closed after an evidence violation',
      );
    }
    if (!this.active || this.finished) {
      throw new TailFreshnessShadowError('shadow scheduler is not active');
    }
  }

  private ensureReadable(): void {
    if (this.failed) {
      throw new TailFreshnessShadowError(
        'shadow scheduler is closed after an evidence violation',
      );
    }
    if (!this.active && !this.finished) {
      throw new TailFreshnessShadowError('shadow scheduler has not started');
    }
  }

  private raise(message: string): never {
    this.failed = true;
    throw new TailFreshnessShadowError(message);
  }
}
