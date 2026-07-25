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

export interface PlaybackScheduleEvent {
  timestampMs: number;
  schedulePerformanceMs: number;
  audioContextTimeAtScheduleSeconds: number;
  scheduledStartContextSeconds: number;
  scheduledEndContextSeconds: number;
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

interface UseAudioPlaybackOptions {
  sampleRate?: number;
  initialMuted?: boolean;
  adaptivePlayback?: boolean;
  playbackPolicy?: PlaybackPolicy;
  onSchedule?: (event: PlaybackScheduleEvent) => void;
}

interface UseAudioPlaybackReturn {
  isPlaying: boolean;
  isMuted: boolean;
  queueAudio: (
    audioData: ArrayBuffer,
    observation?: AudioFrameObservation,
  ) => void;
  start: () => void;
  stop: () => void;
  setMuted: (muted: boolean) => void;
  getPlaybackPosition: () => number;
  getPlaybackMetrics: () => PlaybackMetrics;
}

export function useAudioPlayback({
  sampleRate = 16000,
  initialMuted = false,
  adaptivePlayback = false,
  playbackPolicy = DEFAULT_PLAYBACK_POLICY,
  onSchedule,
}: UseAudioPlaybackOptions = {}): UseAudioPlaybackReturn {
  const [isPlaying, setIsPlaying] = useState(false);
  const [isMuted, setIsMutedState] = useState(initialMuted);

  const audioContextRef = useRef<AudioContext | null>(null);
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
  const adaptivePlaybackRef = useRef(adaptivePlayback);
  const playbackPolicyRef = useRef(playbackPolicy);
  const onScheduleRef = useRef(onSchedule);

  useEffect(() => {
    adaptivePlaybackRef.current = adaptivePlayback;
    playbackPolicyRef.current = playbackPolicy;
    onScheduleRef.current = onSchedule;
  }, [adaptivePlayback, playbackPolicy, onSchedule]);

  const start = useCallback(() => {
    console.log('AudioPlayback: starting');
    if (isActiveRef.current) return;

    sessionGenerationRef.current += 1;
    if (!audioContextRef.current) {
      audioContextRef.current = new AudioContext({ sampleRate });
      console.log('AudioPlayback: created AudioContext, state:', audioContextRef.current.state);
    }

    if (audioContextRef.current.state === 'suspended') {
      console.log('AudioPlayback: resuming suspended AudioContext');
      audioContextRef.current.resume();
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
    isActiveRef.current = true;
    setIsPlaying(true);
  }, [sampleRate]);

  const stop = useCallback(() => {
    console.log('AudioPlayback: stopping');
    isActiveRef.current = false;
    sessionGenerationRef.current += 1;
    setIsPlaying(false);

    gainNodeRef.current = null;

    if (audioContextRef.current) {
      audioContextRef.current.close();
      audioContextRef.current = null;
    }
  }, []);

  const queueAudio = useCallback(
    (
      audioData: ArrayBuffer,
      observation?: AudioFrameObservation,
    ) => {
      if (!isActiveRef.current) {
        return;
      }

      if (!audioContextRef.current) {
        return;
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

      if (gainNodeRef.current) {
        source.connect(gainNodeRef.current);
      } else {
        source.connect(ctx.destination);
      }

      const bufferDuration = audioBuffer.duration;

      // Select a rate from the projected queue depth. The policy deliberately
      // preserves every speech sample; the 10-second limit is an SLA alarm,
      // not a destructive drop boundary.
      const schedulePerformanceMs = performance.now();
      const audioContextTimeAtScheduleSeconds = ctx.currentTime;
      const currentTime = audioContextTimeAtScheduleSeconds;
      const startTime = Math.max(nextStartTimeRef.current, currentTime);
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
      const projectedScheduledStartClientMs = (
        schedulePerformanceMs
        + (startTime - audioContextTimeAtScheduleSeconds) * 1000
      );
      const queueDepthSeconds = Math.max(0, scheduledEndTime - currentTime);
      const aboveTarget = queueDepthSeconds > policy.targetQueueSeconds;
      const aboveLimit = queueDepthSeconds > policy.limitQueueSeconds;

      source.playbackRate.value = playbackRate;

      // Track logical media position in source-audio seconds. This remains
      // independent of the wall-clock playback rate.
      source.onended = () => {
        if (sourceGeneration === sessionGenerationRef.current) {
          playbackPositionRef.current += bufferDuration;
        }
      };

      // Avoid per-buffer console output here: a long sample produces tens of
      // thousands of callbacks, and DevTools logging can distort the latency
      // experiment that this hook is intended to measure.
      source.start(startTime);
      nextStartTimeRef.current = scheduledEndTime;

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

      onScheduleRef.current?.({
        timestampMs: schedulePerformanceMs,
        schedulePerformanceMs,
        audioContextTimeAtScheduleSeconds,
        scheduledStartContextSeconds: startTime,
        scheduledEndContextSeconds: scheduledEndTime,
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
      });
    },
    [sampleRate]
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
    isActiveRef.current = false;
    sessionGenerationRef.current += 1;
    if (audioContextRef.current) {
      audioContextRef.current.close();
      audioContextRef.current = null;
    }
  }, []);

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
