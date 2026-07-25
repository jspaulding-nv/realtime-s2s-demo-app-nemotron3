import { useCallback, useEffect, useRef, useState } from 'react';
import {
  DEFAULT_PLAYBACK_POLICY,
  playbackRateForMode,
  selectPlaybackMode,
  selectPlaybackRate,
  type PlaybackMode,
  type PlaybackPolicy,
} from '../utils/playbackPolicy';
import type { AudioFrameObservation } from '../types/audioMetadata';

const MAX_OUTPUT_TIMESTAMP_STALENESS_MS = 500;

export interface PlaybackScheduleEvent {
  playbackClockSessionId: number;
  timestampMs: number;
  schedulePerformanceMs: number;
  audioContextTimeAtScheduleSeconds: number;
  scheduledStartContextSeconds: number;
  scheduledEndContextSeconds: number;
  scheduledStartContextFrameFloor: number;
  scheduledEndContextFrameExclusive: number;
  projectedScheduledStartClientMs: number;
  audioBytes: number;
  sourceDurationSeconds: number;
  scheduledDurationSeconds: number;
  waitBeforePlaybackSeconds: number;
  queueDepthSeconds: number;
  playbackRate: number;
  playbackMode: PlaybackMode;
  modeChanged: boolean;
  aboveTarget: boolean;
  aboveLimit: boolean;
  audioFrame?: AudioFrameObservation;
}

export type PlaybackClockSampleBasis =
  | 'get_output_timestamp'
  | 'current_time_bracket';

export type PlaybackClockSampleReason =
  | 'session_started'
  | 'queue_started'
  | 'interval'
  | 'queue_drained'
  | 'session_stopped';

export interface PlaybackClockSampleEvent {
  playbackClockSessionId: number;
  clockSampleSequence: number;
  clockSampleReason: PlaybackClockSampleReason;
  clockSamplePerformanceClientMs: number;
  clockSamplePerformanceBeforeClientMs: number;
  clockSamplePerformanceAfterClientMs: number;
  clockSampleContextSeconds: number;
  clockSampleOutputContextSeconds?: number;
  clockSampleOutputPerformanceClientMs?: number;
  clockSampleBasis: PlaybackClockSampleBasis;
  clockSampleQueueEndContextSeconds: number;
}

export interface PlaybackMetrics {
  queueDepthSeconds: number;
  peakQueueDepthSeconds: number;
  playbackRate: number;
  playbackMode: PlaybackMode;
  totalSourceDurationSeconds: number;
  totalScheduledDurationSeconds: number;
  aboveTarget: boolean;
  aboveLimit: boolean;
  limitExceededCount: number;
}

export interface PlaybackStartRouting {
  audioContext: AudioContext;
  captureNode: AudioNode;
  captureInputIndex: number;
}

export interface UseAudioPlaybackOptions {
  sampleRate?: number;
  initialMuted?: boolean;
  adaptivePlayback?: boolean;
  minimumScheduleLeadSeconds?: number;
  quantizeScheduleToSampleFrames?: boolean;
  playbackPolicy?: PlaybackPolicy;
  onSchedule?: (event: PlaybackScheduleEvent) => void;
  onClockSample?: (event: PlaybackClockSampleEvent) => void;
}

export interface UseAudioPlaybackReturn {
  isPlaying: boolean;
  isMuted: boolean;
  queueAudio: (
    audioData: ArrayBuffer,
    observation?: AudioFrameObservation,
  ) => PlaybackScheduleEvent | null;
  start: (routing?: PlaybackStartRouting) => void;
  stop: () => void;
  setMuted: (muted: boolean) => void;
  getPlaybackPosition: () => number;
  getPlaybackMetrics: () => PlaybackMetrics;
}

export function useAudioPlayback({
  sampleRate = 16000,
  initialMuted = false,
  adaptivePlayback = false,
  minimumScheduleLeadSeconds = 0,
  quantizeScheduleToSampleFrames = false,
  playbackPolicy = DEFAULT_PLAYBACK_POLICY,
  onSchedule,
  onClockSample,
}: UseAudioPlaybackOptions = {}): UseAudioPlaybackReturn {
  const [isPlaying, setIsPlaying] = useState(false);
  const [isMuted, setIsMutedState] = useState(initialMuted);

  const audioContextRef = useRef<AudioContext | null>(null);
  const ownsAudioContextRef = useRef(false);
  const captureNodeRef = useRef<AudioNode | null>(null);
  const captureInputIndexRef = useRef<number | null>(null);
  const activeSourcesRef = useRef<Set<AudioBufferSourceNode>>(new Set());
  const nextStartTimeRef = useRef<number>(0);
  const isActiveRef = useRef<boolean>(false);
  const gainNodeRef = useRef<GainNode | null>(null);
  const mutedRef = useRef(initialMuted);
  const playbackPositionRef = useRef(0);
  const peakQueueDepthRef = useRef(0);
  const lastPlaybackRateRef = useRef(playbackPolicy.normalRate);
  const playbackModeRef = useRef<PlaybackMode>('normal');
  const totalSourceDurationRef = useRef(0);
  const totalScheduledDurationRef = useRef(0);
  const limitExceededCountRef = useRef(0);
  const sessionGenerationRef = useRef(0);
  const projectedEndClientMsRef = useRef<number | null>(null);
  const clockSampleTimerRef = useRef<ReturnType<typeof setInterval> | null>(
    null,
  );
  const clockSampleSequenceRef = useRef(0);
  const clockWasQueuedRef = useRef(false);
  const adaptivePlaybackRef = useRef(adaptivePlayback);
  const minimumScheduleLeadSecondsRef = useRef(
    minimumScheduleLeadSeconds,
  );
  const quantizeScheduleToSampleFramesRef = useRef(
    quantizeScheduleToSampleFrames,
  );
  const playbackPolicyRef = useRef(playbackPolicy);
  const onScheduleRef = useRef(onSchedule);
  const onClockSampleRef = useRef(onClockSample);

  useEffect(() => {
    adaptivePlaybackRef.current = adaptivePlayback;
    minimumScheduleLeadSecondsRef.current = minimumScheduleLeadSeconds;
    quantizeScheduleToSampleFramesRef.current = (
      quantizeScheduleToSampleFrames
    );
    playbackPolicyRef.current = playbackPolicy;
    onScheduleRef.current = onSchedule;
    onClockSampleRef.current = onClockSample;
  }, [
    adaptivePlayback,
    minimumScheduleLeadSeconds,
    quantizeScheduleToSampleFrames,
    playbackPolicy,
    onSchedule,
    onClockSample,
  ]);

  const emitClockSample = useCallback((
    ctx: AudioContext,
    reason: PlaybackClockSampleReason,
  ) => {
    if (!onClockSampleRef.current) return;

    const performanceBeforeClientMs = performance.now();
    const contextSeconds = ctx.currentTime;
    let candidateOutputContextSeconds: number | undefined;
    let candidateOutputPerformanceClientMs: number | undefined;

    try {
      if (typeof ctx.getOutputTimestamp === 'function') {
        const outputTimestamp = ctx.getOutputTimestamp();
        const outputContextTime = outputTimestamp.contextTime;
        const outputPerformanceTime = outputTimestamp.performanceTime;
        // Browsers may expose the method before the rendering clock is
        // initialized and return an all-zero placeholder. Do not label that
        // placeholder as stronger output-timestamp evidence.
        if (
          typeof outputContextTime === 'number'
          && Number.isFinite(outputContextTime)
          && outputContextTime > 0
          && typeof outputPerformanceTime === 'number'
          && Number.isFinite(outputPerformanceTime)
          && outputPerformanceTime > 0
        ) {
          candidateOutputContextSeconds = outputContextTime;
          candidateOutputPerformanceClientMs = outputPerformanceTime;
        }
      }
    } catch {
      // Preserve the currentTime/performance.now bracket as the portable
      // fallback when getOutputTimestamp is unavailable or fails.
    }

    const performanceAfterClientMs = performance.now();
    const outputTimestampIsCurrent = (
      candidateOutputContextSeconds !== undefined
      && candidateOutputPerformanceClientMs !== undefined
      && candidateOutputContextSeconds <= contextSeconds
      && (
        contextSeconds - candidateOutputContextSeconds
      ) * 1000 <= MAX_OUTPUT_TIMESTAMP_STALENESS_MS
      && (
        candidateOutputPerformanceClientMs
        <= performanceAfterClientMs
      )
      && (
        performanceAfterClientMs
        - candidateOutputPerformanceClientMs
      ) <= MAX_OUTPUT_TIMESTAMP_STALENESS_MS
    );
    const basis: PlaybackClockSampleBasis = outputTimestampIsCurrent
      ? 'get_output_timestamp'
      : 'current_time_bracket';

    onClockSampleRef.current({
      playbackClockSessionId: sessionGenerationRef.current,
      clockSampleSequence: clockSampleSequenceRef.current,
      clockSampleReason: reason,
      clockSamplePerformanceClientMs: (
        performanceBeforeClientMs
        + (performanceAfterClientMs - performanceBeforeClientMs) / 2
      ),
      clockSamplePerformanceBeforeClientMs: performanceBeforeClientMs,
      clockSamplePerformanceAfterClientMs: performanceAfterClientMs,
      clockSampleContextSeconds: contextSeconds,
      ...(basis === 'get_output_timestamp'
        ? {
            clockSampleOutputContextSeconds: (
              candidateOutputContextSeconds
            ),
            clockSampleOutputPerformanceClientMs: (
              candidateOutputPerformanceClientMs
            ),
          }
        : {}),
      clockSampleBasis: basis,
      clockSampleQueueEndContextSeconds: nextStartTimeRef.current,
    });
    clockSampleSequenceRef.current += 1;
  }, []);

  const clearClockSampleTimer = useCallback(() => {
    if (clockSampleTimerRef.current !== null) {
      clearInterval(clockSampleTimerRef.current);
      clockSampleTimerRef.current = null;
    }
  }, []);

  const start = useCallback((routing?: PlaybackStartRouting) => {
    console.log('AudioPlayback: starting');
    if (isActiveRef.current) return;

    sessionGenerationRef.current += 1;
    if (routing) {
      if (
        routing.audioContext.sampleRate !== sampleRate
        || !Number.isSafeInteger(routing.captureInputIndex)
        || routing.captureInputIndex < 0
      ) {
        throw new Error(
          'Common-clock playback routing does not match the PCM sample rate.',
        );
      }
      audioContextRef.current = routing.audioContext;
      ownsAudioContextRef.current = false;
      captureNodeRef.current = routing.captureNode;
      captureInputIndexRef.current = routing.captureInputIndex;
    } else if (!audioContextRef.current) {
      audioContextRef.current = new AudioContext({ sampleRate });
      ownsAudioContextRef.current = true;
      captureNodeRef.current = null;
      captureInputIndexRef.current = null;
      console.log('AudioPlayback: created AudioContext, state:', audioContextRef.current.state);
    }

    if (audioContextRef.current.state === 'suspended') {
      console.log('AudioPlayback: resuming suspended AudioContext');
      void audioContextRef.current.resume();
    }

    const gainNode = audioContextRef.current.createGain();
    gainNode.gain.value = mutedRef.current ? 0 : 1;
    gainNode.connect(audioContextRef.current.destination);
    gainNodeRef.current = gainNode;

    nextStartTimeRef.current = audioContextRef.current.currentTime;
    playbackPositionRef.current = 0;
    peakQueueDepthRef.current = 0;
    lastPlaybackRateRef.current = playbackPolicyRef.current.normalRate;
    playbackModeRef.current = 'normal';
    totalSourceDurationRef.current = 0;
    totalScheduledDurationRef.current = 0;
    limitExceededCountRef.current = 0;
    projectedEndClientMsRef.current = null;
    clockSampleSequenceRef.current = 0;
    clockWasQueuedRef.current = false;
    isActiveRef.current = true;
    emitClockSample(audioContextRef.current, 'session_started');
    clearClockSampleTimer();
    if (onClockSampleRef.current) {
      clockSampleTimerRef.current = setInterval(() => {
        const ctx = audioContextRef.current;
        if (!isActiveRef.current || !ctx) return;

        const queueIsActive = nextStartTimeRef.current > ctx.currentTime;
        if (queueIsActive) {
          clockWasQueuedRef.current = true;
          emitClockSample(ctx, 'interval');
        } else if (clockWasQueuedRef.current) {
          emitClockSample(ctx, 'queue_drained');
          clockWasQueuedRef.current = false;
        }
      }, 200);
    }
    setIsPlaying(true);
  }, [clearClockSampleTimer, emitClockSample, sampleRate]);

  const stop = useCallback(() => {
    console.log('AudioPlayback: stopping');
    if (audioContextRef.current && isActiveRef.current) {
      if (
        clockWasQueuedRef.current
        && nextStartTimeRef.current <= audioContextRef.current.currentTime
      ) {
        // Teardown can race the 200 ms sampler immediately after a true
        // drain. Close that interval only when the AudioContext clock proves
        // the scheduled endpoint has passed.
        emitClockSample(audioContextRef.current, 'queue_drained');
        clockWasQueuedRef.current = false;
      }
      emitClockSample(audioContextRef.current, 'session_stopped');
    }
    clearClockSampleTimer();
    isActiveRef.current = false;
    sessionGenerationRef.current += 1;
    projectedEndClientMsRef.current = null;
    setIsPlaying(false);

    for (const source of activeSourcesRef.current) {
      try {
        source.stop();
      } catch {
        // An already-ended one-shot source is safe to ignore during teardown.
      }
      try {
        source.disconnect();
      } catch {
        // The node may already be detached by its owning AudioContext.
      }
    }
    activeSourcesRef.current.clear();
    gainNodeRef.current = null;
    captureNodeRef.current = null;
    captureInputIndexRef.current = null;

    if (audioContextRef.current) {
      if (ownsAudioContextRef.current) {
        void audioContextRef.current.close();
      }
      audioContextRef.current = null;
    }
    ownsAudioContextRef.current = false;
  }, [clearClockSampleTimer, emitClockSample]);

  const queueAudio = useCallback(
    (
      audioData: ArrayBuffer,
      observation?: AudioFrameObservation,
    ) => {
      if (!isActiveRef.current) {
        return null;
      }

      if (!audioContextRef.current) {
        return null;
      }

      const ctx = audioContextRef.current;

      // Convert Int16 PCM to Float32 for Web Audio API
      const int16Array = new Int16Array(audioData);
      const float32Array = new Float32Array(int16Array.length);

      for (let i = 0; i < int16Array.length; i++) {
        float32Array[i] = int16Array[i] / 32768.0;
      }

      // Create audio buffer
      const audioBuffer = ctx.createBuffer(1, float32Array.length, sampleRate);
      audioBuffer.getChannelData(0).set(float32Array);

      // Create buffer source and route through gain node
      const source = ctx.createBufferSource();
      source.buffer = audioBuffer;
      const sourceGeneration = sessionGenerationRef.current;
      activeSourcesRef.current.add(source);

      if (gainNodeRef.current) {
        source.connect(gainNodeRef.current);
      } else {
        source.connect(ctx.destination);
      }
      if (
        captureNodeRef.current
        && captureInputIndexRef.current !== null
      ) {
        // The capture branch is deliberately before the user-facing monitor
        // mute gain. AudioBufferSourceNode playbackRate has already been
        // applied at this output, so translated channel evidence represents
        // the actual queued/rate-adjusted program stream.
        source.connect(
          captureNodeRef.current,
          0,
          captureInputIndexRef.current,
        );
      }

      const bufferDuration = audioBuffer.duration;

      // Select a rate from the projected queue depth. The policy deliberately
      // preserves every speech sample; the 10-second limit is an SLA alarm,
      // not a destructive drop boundary.
      const schedulePerformanceMs = performance.now();
      const audioContextTimeAtScheduleSeconds = ctx.currentTime;
      const currentTime = audioContextTimeAtScheduleSeconds;
      const requestedStartTime = Math.max(
        nextStartTimeRef.current,
        currentTime + minimumScheduleLeadSecondsRef.current,
      );
      const quantizedStartFrame = quantizeScheduleToSampleFramesRef.current
        ? Math.ceil(requestedStartTime * ctx.sampleRate)
        : null;
      const startTime = quantizedStartFrame === null
        ? requestedStartTime
        : quantizedStartFrame / ctx.sampleRate;
      const waitBeforePlaybackSeconds = Math.max(0, startTime - currentTime);
      const projectedQueueAtNormalRate = waitBeforePlaybackSeconds + bufferDuration;
      const policy = playbackPolicyRef.current;
      const previousMode = playbackModeRef.current;
      const playbackMode = adaptivePlaybackRef.current
        ? selectPlaybackMode(projectedQueueAtNormalRate, previousMode, policy)
        : 'normal';
      const playbackRate = adaptivePlaybackRef.current
        ? playbackRateForMode(playbackMode, policy)
        : selectPlaybackRate(0, policy);
      const scheduledDuration = bufferDuration / playbackRate;
      const scheduledEndTime = startTime + scheduledDuration;
      const queueWasActive = nextStartTimeRef.current > currentTime;
      if (!queueWasActive && clockWasQueuedRef.current) {
        // The previous queue may have drained between interval ticks. Preserve
        // its old endpoint before replacing nextStartTime with this buffer's
        // schedule so every disjoint queued interval has an explicit close.
        emitClockSample(ctx, 'queue_drained');
        clockWasQueuedRef.current = false;
      }
      const projectedScheduledStartClientMs = Math.max(
        schedulePerformanceMs,
        projectedEndClientMsRef.current ?? schedulePerformanceMs,
      );
      const projectedScheduledEndClientMs = (
        projectedScheduledStartClientMs + scheduledDuration * 1000
      );
      const queueDepthSeconds = Math.max(0, scheduledEndTime - currentTime);
      const aboveTarget = queueDepthSeconds > policy.targetQueueSeconds;
      const aboveLimit = queueDepthSeconds > policy.limitQueueSeconds;

      source.playbackRate.value = playbackRate;

      // Track logical media position in source-audio seconds. This remains
      // independent of the wall-clock playback rate.
      source.onended = () => {
        activeSourcesRef.current.delete(source);
        if (sourceGeneration === sessionGenerationRef.current) {
          playbackPositionRef.current += bufferDuration;
        }
      };

      // Avoid per-buffer console output here: a long sample produces tens of
      // thousands of callbacks, and DevTools logging can distort the latency
      // experiment that this hook is intended to measure.
      source.start(startTime);
      nextStartTimeRef.current = scheduledEndTime;
      projectedEndClientMsRef.current = projectedScheduledEndClientMs;
      if (!queueWasActive) {
        clockWasQueuedRef.current = true;
        emitClockSample(ctx, 'queue_started');
      }

      peakQueueDepthRef.current = Math.max(
        peakQueueDepthRef.current,
        queueDepthSeconds,
      );
      lastPlaybackRateRef.current = playbackRate;
      totalSourceDurationRef.current += bufferDuration;
      totalScheduledDurationRef.current += scheduledDuration;
      const modeChanged = playbackMode !== previousMode;
      if (playbackMode === 'over-limit' && previousMode !== 'over-limit') {
        limitExceededCountRef.current += 1;
      }
      playbackModeRef.current = playbackMode;

      const scheduleEvent: PlaybackScheduleEvent = {
        playbackClockSessionId: sessionGenerationRef.current,
        timestampMs: schedulePerformanceMs,
        schedulePerformanceMs,
        audioContextTimeAtScheduleSeconds,
        scheduledStartContextSeconds: startTime,
        scheduledEndContextSeconds: scheduledEndTime,
        scheduledStartContextFrameFloor: (
          quantizedStartFrame
          ?? Math.floor(startTime * ctx.sampleRate)
        ),
        scheduledEndContextFrameExclusive: (
          quantizedStartFrame === null
            ? Math.ceil(scheduledEndTime * ctx.sampleRate)
            : (
                quantizedStartFrame
                + Math.ceil(float32Array.length / playbackRate)
              )
        ),
        projectedScheduledStartClientMs,
        audioBytes: audioData.byteLength,
        sourceDurationSeconds: bufferDuration,
        scheduledDurationSeconds: scheduledDuration,
        waitBeforePlaybackSeconds,
        queueDepthSeconds,
        playbackRate,
        playbackMode,
        modeChanged,
        aboveTarget,
        aboveLimit,
        ...(observation ? { audioFrame: observation } : {}),
      };
      onScheduleRef.current?.(scheduleEvent);
      return scheduleEvent;
    },
    [emitClockSample, sampleRate]
  );

  const setMuted = useCallback((muted: boolean) => {
    mutedRef.current = muted;
    setIsMutedState(muted);
    if (gainNodeRef.current) {
      gainNodeRef.current.gain.value = muted ? 0 : 1;
    }
  }, []);

  const getPlaybackPosition = useCallback(() => playbackPositionRef.current, []);

  const getPlaybackMetrics = useCallback((): PlaybackMetrics => {
    const ctx = audioContextRef.current;
    const policy = playbackPolicyRef.current;
    const queueDepthSeconds = ctx
      ? Math.max(0, nextStartTimeRef.current - ctx.currentTime)
      : 0;

    return {
      queueDepthSeconds,
      peakQueueDepthSeconds: peakQueueDepthRef.current,
      playbackRate: lastPlaybackRateRef.current,
      playbackMode: playbackModeRef.current,
      totalSourceDurationSeconds: totalSourceDurationRef.current,
      totalScheduledDurationSeconds: totalScheduledDurationRef.current,
      aboveTarget: queueDepthSeconds > policy.targetQueueSeconds,
      aboveLimit: queueDepthSeconds > policy.limitQueueSeconds,
      limitExceededCount: limitExceededCountRef.current,
    };
  }, []);

  useEffect(() => () => {
    clearClockSampleTimer();
    isActiveRef.current = false;
    sessionGenerationRef.current += 1;
    projectedEndClientMsRef.current = null;
    for (const source of activeSourcesRef.current) {
      try {
        source.stop();
      } catch {
        // Ignore already-ended sources while unmounting.
      }
      try {
        source.disconnect();
      } catch {
        // Ignore nodes detached by context teardown.
      }
    }
    activeSourcesRef.current.clear();
    if (audioContextRef.current) {
      if (ownsAudioContextRef.current) {
        void audioContextRef.current.close();
      }
      audioContextRef.current = null;
    }
    ownsAudioContextRef.current = false;
    captureNodeRef.current = null;
    captureInputIndexRef.current = null;
  }, [clearClockSampleTimer]);

  return {
    isPlaying,
    isMuted,
    queueAudio,
    start,
    stop,
    setMuted,
    getPlaybackPosition,
    getPlaybackMetrics,
  };
}
