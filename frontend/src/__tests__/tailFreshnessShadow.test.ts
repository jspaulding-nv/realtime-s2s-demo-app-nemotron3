import { describe, expect, it } from 'vitest';
import type {
  AudioFrameMetadata,
  AudioFrameObservation,
  AudioParentCompleteObservation,
} from '../types/audioMetadata';
import {
  TailFreshnessShadowError,
  TailFreshnessShadowScheduler,
} from '../utils/tailFreshnessShadow';

function frame(
  parentSequenceId: number,
  audioFrameId: number,
  atSeconds: number,
  durationSeconds = 0.5,
): Parameters<TailFreshnessShadowScheduler['observeFrame']>[0] {
  const audioBytes = Math.round(durationSeconds * 16_000 * 2);
  const metadata: AudioFrameMetadata = {
    type: 'audio_frame',
    protocolVersion: 1,
    streamGeneration: 1,
    parentSequenceId,
    audioFrameId,
    audioBytes,
    sampleRateHz: 16_000,
    channels: 1,
    bytesPerSample: 2,
    sourceStartMs: null,
    sourceEndMs: null,
  };
  const observation: AudioFrameObservation = {
    metadata,
    binaryReceivedAtMs: atSeconds * 1_000,
  };
  return {
    observation,
    schedulePerformanceMs: atSeconds * 1_000,
    audioContextTimeAtScheduleSeconds: atSeconds,
    audioBytes,
    sourceDurationSeconds: durationSeconds,
  };
}

function complete(
  parentSequenceId: number,
  frameCount: number,
  durationSeconds = 0.5,
): AudioParentCompleteObservation {
  return {
    metadata: {
      type: 'audio_parent_complete',
      protocolVersion: 1,
      streamGeneration: 1,
      parentSequenceId,
      audioFrameCount: frameCount,
      audioBytes: Math.round(frameCount * durationSeconds * 16_000 * 2),
      sourceStartMs: null,
      sourceEndMs: null,
    },
    receivedAtMs: 1_000,
  };
}

describe('TailFreshnessShadowScheduler', () => {
  it('keeps one prefix and suppresses all later frames in that parent', () => {
    const shadow = new TailFreshnessShadowScheduler({
      hardCapSeconds: 1.2,
      cancellationGuardSeconds: 0,
      adaptivePlayback: false,
    });
    shadow.begin();

    const decisions = [
      shadow.observeFrame(frame(0, 0, 0)),
      shadow.observeFrame(frame(0, 1, 0.1)),
      shadow.observeFrame(frame(0, 2, 0.2)),
      shadow.observeFrame(frame(0, 3, 0.3)),
    ];
    shadow.observeParentComplete(complete(0, 4));
    const evidence = shadow.finish();

    expect(decisions[2]).toMatchObject({
      truncationTriggered: true,
      suppressedArrival: false,
      droppedFrameCountThisEvent: 1,
    });
    expect(decisions[3]).toMatchObject({
      truncationTriggered: false,
      suppressedArrival: true,
      droppedFrameCountThisEvent: 1,
    });
    expect(evidence.summary).toMatchObject({
      framesReceived: 4,
      framesRetained: 2,
      framesDropped: 2,
      parentsTruncated: 1,
      hardCapAchieved: true,
      singleTailContractHolds: true,
    });
    expect(evidence.parents).toEqual([
      expect.objectContaining({
        parentSequenceId: 0,
        retainedFrameCount: 2,
        droppedFrameCount: 2,
        firstDroppedFrameId: 2,
        shape: 'partial_suffix',
      }),
    ]);
    expect(evidence.observationOnly).toBe(true);
    expect(evidence.liveAudioChanged).toBe(false);
    expect(evidence.containsPcm).toBe(false);
  });

  it('can fully drop an unstarted complete parent without breaking the cap', () => {
    const shadow = new TailFreshnessShadowScheduler({
      hardCapSeconds: 3.9,
      cancellationGuardSeconds: 0,
      adaptivePlayback: false,
    });
    shadow.begin();
    shadow.observeFrame(frame(0, 0, 0, 2));
    shadow.observeParentComplete(complete(0, 1, 2));
    shadow.observeFrame(frame(1, 0, 0.1, 2));
    shadow.observeParentComplete(complete(1, 1, 2));
    shadow.observeFrame(frame(2, 0, 0.2, 2));
    shadow.observeParentComplete(complete(2, 1, 2));

    const evidence = shadow.finish();
    expect(evidence.summary.parentsFullyDropped).toBe(1);
    expect(evidence.parents[1].shape).toBe('fully_dropped');
    expect(evidence.summary.peakQueueAfterTruncationSeconds).toBeLessThanOrEqual(
      3.9,
    );
  });

  it('reports a residual breach when every queued frame is guarded', () => {
    const shadow = new TailFreshnessShadowScheduler({
      hardCapSeconds: 0.7,
      cancellationGuardSeconds: 0.4,
      adaptivePlayback: false,
    });
    shadow.begin();
    shadow.observeFrame(frame(0, 0, 0));
    const decision = shadow.observeFrame(frame(0, 1, 0.1));
    shadow.observeParentComplete(complete(0, 2));
    const evidence = shadow.finish();

    expect(decision.residualOverCapSeconds).toBeGreaterThan(0);
    expect(evidence.summary.hardCapAchieved).toBe(false);
    expect(evidence.summary.residualBreachEvents).toBe(1);
  });

  it('latches closed after non-contiguous frame evidence', () => {
    const shadow = new TailFreshnessShadowScheduler();
    shadow.begin();

    expect(() => shadow.observeFrame(frame(0, 1, 0))).toThrow(
      TailFreshnessShadowError,
    );
    expect(() => shadow.observeFrame(frame(0, 0, 0.1))).toThrow(
      /closed after an evidence violation/,
    );
  });

  it('requires the parent completion marker before final evidence', () => {
    const shadow = new TailFreshnessShadowScheduler();
    shadow.begin();
    shadow.observeFrame(frame(0, 0, 0));

    expect(() => shadow.finish()).toThrow(
      /before the active parent completed/,
    );
  });
});
