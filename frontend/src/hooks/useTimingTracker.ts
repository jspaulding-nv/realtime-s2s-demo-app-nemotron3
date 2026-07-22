import { useCallback, useMemo, useRef } from 'react';
import type { ClientTimingEvent } from '../types/timing';
import type { PlaybackScheduleEvent } from './useAudioPlayback';
import type { PlaybackMode } from '../utils/playbackPolicy';

interface UseTimingTrackerReturn {
  startTest: (configuration?: { adaptivePlaybackEnabled: boolean }) => void;
  logChunkSent: (audioBytes: number) => void;
  logAudioReceived: (audioBytes: number) => void;
  logPlaybackScheduled: (event: PlaybackScheduleEvent) => void;
  logPlaybackQueueSample: (
    queueDepthSec: number,
    playbackRate: number,
    playbackMode: PlaybackMode,
  ) => void;
  getEvents: () => ClientTimingEvent[];
  getSendCount: () => number;
  getReceiveCount: () => number;
  getSourcePosition: () => number;
  getCumulativeOutputDuration: () => number;
}

const CHUNK_SIZE = 4800;
const SAMPLE_RATE = 16000;
const BYTES_PER_SAMPLE = 2; // Int16

export function useTimingTracker(): UseTimingTrackerReturn {
  const eventsRef = useRef<ClientTimingEvent[]>([]);
  const sendCountRef = useRef(0);
  const receiveCountRef = useRef(0);
  const cumulativeOutputSamplesRef = useRef(0);
  const testStartRef = useRef(0);

  const startTest = useCallback((configuration?: {
    adaptivePlaybackEnabled: boolean;
  }) => {
    eventsRef.current = [];
    sendCountRef.current = 0;
    receiveCountRef.current = 0;
    cumulativeOutputSamplesRef.current = 0;
    testStartRef.current = performance.now();
    if (configuration) {
      eventsRef.current.push({
        stage: 'playback_session_started',
        timestamp: 0,
        chunkIndex: -1,
        sourcePositionSec: 0,
        audioBytes: 0,
        adaptivePlaybackEnabled: configuration.adaptivePlaybackEnabled,
      });
    }
  }, []);

  const logChunkSent = useCallback((audioBytes: number) => {
    const idx = sendCountRef.current;
    sendCountRef.current += 1;
    eventsRef.current.push({
      stage: 'chunk_sent',
      timestamp: performance.now() - testStartRef.current,
      chunkIndex: idx,
      sourcePositionSec: idx * (CHUNK_SIZE / SAMPLE_RATE),
      audioBytes,
    });
  }, []);

  const logAudioReceived = useCallback((audioBytes: number) => {
    const idx = receiveCountRef.current;
    receiveCountRef.current += 1;
    const samples = audioBytes / BYTES_PER_SAMPLE;
    cumulativeOutputSamplesRef.current += samples;
    eventsRef.current.push({
      stage: 'audio_received',
      timestamp: performance.now() - testStartRef.current,
      chunkIndex: idx,
      sourcePositionSec: 0, // not applicable for received audio
      audioBytes,
    });
  }, []);

  const logPlaybackScheduled = useCallback((event: PlaybackScheduleEvent) => {
    eventsRef.current.push({
      stage: 'playback_chunk_scheduled',
      timestamp: performance.now() - testStartRef.current,
      chunkIndex: receiveCountRef.current - 1,
      sourcePositionSec: 0,
      audioBytes: event.audioBytes,
      mediaDurationSec: event.sourceDurationSeconds,
      scheduledDurationSec: event.scheduledDurationSeconds,
      playbackWaitSec: event.waitBeforePlaybackSeconds,
      queueDepthSec: event.queueDepthSeconds,
      playbackRate: event.playbackRate,
      playbackMode: event.playbackMode,
    });
  }, []);

  const logPlaybackQueueSample = useCallback((
    queueDepthSec: number,
    playbackRate: number,
    playbackMode: PlaybackMode,
  ) => {
    eventsRef.current.push({
      stage: 'playback_queue_sample',
      timestamp: performance.now() - testStartRef.current,
      chunkIndex: -1,
      sourcePositionSec: 0,
      audioBytes: 0,
      queueDepthSec,
      playbackRate,
      playbackMode,
    });
  }, []);

  const getEvents = useCallback(() => [...eventsRef.current], []);
  const getSendCount = useCallback(() => sendCountRef.current, []);
  const getReceiveCount = useCallback(() => receiveCountRef.current, []);
  const getSourcePosition = useCallback(
    () => sendCountRef.current * (CHUNK_SIZE / SAMPLE_RATE),
    [],
  );
  const getCumulativeOutputDuration = useCallback(
    () => cumulativeOutputSamplesRef.current / SAMPLE_RATE,
    [],
  );

  return useMemo(() => ({
    startTest,
    logChunkSent,
    logAudioReceived,
    logPlaybackScheduled,
    logPlaybackQueueSample,
    getEvents,
    getSendCount,
    getReceiveCount,
    getSourcePosition,
    getCumulativeOutputDuration,
  }), [startTest, logChunkSent, logAudioReceived, logPlaybackScheduled, logPlaybackQueueSample, getEvents, getSendCount, getReceiveCount, getSourcePosition, getCumulativeOutputDuration]);
}
