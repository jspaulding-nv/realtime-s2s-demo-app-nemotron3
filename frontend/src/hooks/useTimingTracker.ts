import { useCallback, useMemo, useRef } from 'react';
import type {
  ClientTimingEvent,
  FileAudioChunkObservation,
  SourceTimingBasis,
} from '../types/timing';
import type {
  AudioFrameObservation,
  AudioParentCompleteObservation,
  AudioSourceRange,
} from '../types/audioMetadata';
import type { PlaybackScheduleEvent } from './useAudioPlayback';
import type { PlaybackMode } from '../utils/playbackPolicy';

interface UseTimingTrackerReturn {
  startTest: (configuration?: { adaptivePlaybackEnabled: boolean }) => void;
  logChunkSent: (
    audioBytes: number,
    observation?: FileAudioChunkObservation,
  ) => void;
  logAudioReceived: (
    audioBytes: number,
    observation?: AudioFrameObservation,
  ) => void;
  logAudioParentComplete: (
    observation: AudioParentCompleteObservation,
  ) => void;
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
const SOURCE_LEDGER_TOLERANCE_MS = 0.001;

function sourceTimingBasis(range: AudioSourceRange): SourceTimingBasis {
  if (range.sourceStartMs !== null && range.sourceEndMs !== null) {
    return 'attributed_range';
  }
  if (range.sourceStartMs === null && range.sourceEndMs !== null) {
    return 'audio_processed/nonsemantic';
  }
  if (range.sourceStartMs === null && range.sourceEndMs === null) {
    return 'unavailable';
  }
  return 'partial_range';
}

export function useTimingTracker(): UseTimingTrackerReturn {
  const eventsRef = useRef<ClientTimingEvent[]>([]);
  const sendCountRef = useRef(0);
  const receiveCountRef = useRef(0);
  const cumulativeOutputDurationRef = useRef(0);
  const sourcePositionRef = useRef(0);
  const testStartRef = useRef(0);

  const inputLedgerStartedRef = useRef(false);
  const inputLedgerValidRef = useRef(false);
  const inputSampleZeroClientMsRef = useRef<number | null>(null);
  const inputSampleRateHzRef = useRef<number | null>(null);
  const inputLastSampleEndRef = useRef(0);
  const inputLastEmittedAtMsRef = useRef<number | null>(null);

  const startTest = useCallback((configuration?: {
    adaptivePlaybackEnabled: boolean;
  }) => {
    eventsRef.current = [];
    sendCountRef.current = 0;
    receiveCountRef.current = 0;
    cumulativeOutputDurationRef.current = 0;
    sourcePositionRef.current = 0;
    inputLedgerStartedRef.current = false;
    inputLedgerValidRef.current = false;
    inputSampleZeroClientMsRef.current = null;
    inputSampleRateHzRef.current = null;
    inputLastSampleEndRef.current = 0;
    inputLastEmittedAtMsRef.current = null;
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

  const logChunkSent = useCallback((
    audioBytes: number,
    observation?: FileAudioChunkObservation,
  ) => {
    const idx = sendCountRef.current;
    sendCountRef.current += 1;

    let timestamp = performance.now() - testStartRef.current;
    let sourcePositionSec = idx * (CHUNK_SIZE / SAMPLE_RATE);
    let observationFields: Partial<ClientTimingEvent> = {};

    if (observation) {
      const finiteNumbers = (
        Number.isFinite(observation.emittedAtMs)
        && Number.isFinite(observation.inputSampleZeroClientMs)
      );
      const integerLedger = (
        Number.isSafeInteger(observation.chunkIndex)
        && observation.chunkIndex >= 0
        && Number.isSafeInteger(observation.sampleRateHz)
        && observation.sampleRateHz > 0
        && Number.isSafeInteger(observation.sourceSampleStart)
        && observation.sourceSampleStart >= 0
        && Number.isSafeInteger(observation.sourceSampleEndExclusive)
        && observation.sourceSampleEndExclusive
          > observation.sourceSampleStart
      );
      const isFirst = !inputLedgerStartedRef.current;
      const firstIsValid = (
        isFirst
        && observation.chunkIndex === 0
        && observation.sourceSampleStart === 0
        && observation.inputSampleZeroClientMs === observation.emittedAtMs
      );
      const continuationIsValid = (
        !isFirst
        && inputLedgerValidRef.current
        && observation.chunkIndex === idx
        && observation.sampleRateHz === inputSampleRateHzRef.current
        && observation.sourceSampleStart === inputLastSampleEndRef.current
        && observation.inputSampleZeroClientMs
          === inputSampleZeroClientMsRef.current
        && (
          inputLastEmittedAtMsRef.current === null
          || observation.emittedAtMs >= inputLastEmittedAtMsRef.current
        )
      );
      const sampleCount = (
        observation.sourceSampleEndExclusive
        - observation.sourceSampleStart
      );
      const bytesMatchLedger = audioBytes === sampleCount * BYTES_PER_SAMPLE;
      const valid = (
        finiteNumbers
        && integerLedger
        && bytesMatchLedger
        && (firstIsValid || continuationIsValid)
      );

      inputLedgerStartedRef.current = true;
      inputLedgerValidRef.current = valid;
      if (valid) {
        if (isFirst) {
          inputSampleZeroClientMsRef.current = (
            observation.inputSampleZeroClientMs
          );
          inputSampleRateHzRef.current = observation.sampleRateHz;
        }
        inputLastSampleEndRef.current = observation.sourceSampleEndExclusive;
        inputLastEmittedAtMsRef.current = observation.emittedAtMs;
        sourcePositionRef.current = (
          observation.sourceSampleEndExclusive / observation.sampleRateHz
        );
      }

      timestamp = observation.emittedAtMs - testStartRef.current;
      sourcePositionSec = (
        observation.sourceSampleStart / observation.sampleRateHz
      );
      observationFields = {
        inputSampleZeroClientMs: observation.inputSampleZeroClientMs,
        inputChunkEmittedClientMs: observation.emittedAtMs,
        inputSourceSampleStart: observation.sourceSampleStart,
        inputSourceSampleEndExclusive: (
          observation.sourceSampleEndExclusive
        ),
        inputSampleRateHz: observation.sampleRateHz,
        inputLedgerValid: valid,
      };
    } else {
      sourcePositionRef.current = (
        sendCountRef.current * (CHUNK_SIZE / SAMPLE_RATE)
      );
    }

    eventsRef.current.push({
      stage: 'chunk_sent',
      timestamp,
      chunkIndex: idx,
      sourcePositionSec,
      audioBytes,
      ...observationFields,
    });
  }, []);

  const sourceEndMetrics = useCallback((
    sourceEndMs: number | null,
    observedClientMs?: number,
  ): Partial<ClientTimingEvent> => {
    const anchor = inputSampleZeroClientMsRef.current;
    const sampleRate = inputSampleRateHzRef.current;
    if (
      !inputLedgerValidRef.current
      || anchor === null
      || sampleRate === null
      || sourceEndMs === null
      || !Number.isFinite(sourceEndMs)
      || sourceEndMs < 0
    ) {
      return {};
    }

    const recordedSourceEndMs = (
      inputLastSampleEndRef.current / sampleRate * 1000
    );
    if (sourceEndMs > recordedSourceEndMs + SOURCE_LEDGER_TOLERANCE_MS) {
      return {};
    }

    const sourceEndBoundaryClientMs = anchor + sourceEndMs;
    return {
      inputSampleZeroClientMs: anchor,
      inputLedgerValid: true,
      sourceEndBoundaryClientMs,
      ...(observedClientMs === undefined
        ? {}
        : {
            sourceEndToBinaryReceiptMs: (
              observedClientMs - sourceEndBoundaryClientMs
            ),
          }),
    };
  }, []);

  const logAudioReceived = useCallback((
    audioBytes: number,
    observation?: AudioFrameObservation,
  ) => {
    const idx = receiveCountRef.current;
    receiveCountRef.current += 1;
    const bytesPerAudioFrame = observation
      ? (
          observation.metadata.bytesPerSample
          * observation.metadata.channels
        )
      : BYTES_PER_SAMPLE;
    const outputSampleRate = observation?.metadata.sampleRateHz ?? SAMPLE_RATE;
    cumulativeOutputDurationRef.current += (
      audioBytes / bytesPerAudioFrame / outputSampleRate
    );

    const observedClientMs = (
      observation?.binaryReceivedAtMs ?? performance.now()
    );
    const metadata = observation?.metadata;
    eventsRef.current.push({
      stage: 'audio_received',
      timestamp: observedClientMs - testStartRef.current,
      chunkIndex: idx,
      sourcePositionSec: 0,
      audioBytes,
      ...(metadata
        ? {
            audioMetadataProtocolVersion: metadata.protocolVersion,
            streamGeneration: metadata.streamGeneration,
            parentSequenceId: metadata.parentSequenceId,
            audioFrameId: metadata.audioFrameId,
            sourceStartMs: metadata.sourceStartMs,
            sourceEndMs: metadata.sourceEndMs,
            sourceTimingBasis: sourceTimingBasis(metadata),
            binaryReceiptClientMs: observation.binaryReceivedAtMs,
            ...sourceEndMetrics(
              metadata.sourceEndMs,
              observation.binaryReceivedAtMs,
            ),
          }
        : {}),
    });
  }, [sourceEndMetrics]);

  const logAudioParentComplete = useCallback((
    observation: AudioParentCompleteObservation,
  ) => {
    const metadata = observation.metadata;
    const sourceMetrics = sourceEndMetrics(metadata.sourceEndMs);
    eventsRef.current.push({
      stage: 'audio_parent_complete',
      timestamp: observation.receivedAtMs - testStartRef.current,
      chunkIndex: -1,
      sourcePositionSec: 0,
      audioBytes: metadata.audioBytes,
      audioMetadataProtocolVersion: metadata.protocolVersion,
      streamGeneration: metadata.streamGeneration,
      parentSequenceId: metadata.parentSequenceId,
      audioFrameCount: metadata.audioFrameCount,
      sourceStartMs: metadata.sourceStartMs,
      sourceEndMs: metadata.sourceEndMs,
      sourceTimingBasis: sourceTimingBasis(metadata),
      parentCompleteReceivedClientMs: observation.receivedAtMs,
      ...sourceMetrics,
      ...(sourceMetrics.sourceEndBoundaryClientMs === undefined
        ? {}
        : {
            sourceEndToParentCompleteMs: (
              observation.receivedAtMs
              - sourceMetrics.sourceEndBoundaryClientMs
            ),
          }),
    });
  }, [sourceEndMetrics]);

  const logPlaybackScheduled = useCallback((event: PlaybackScheduleEvent) => {
    const metadata = event.audioFrame?.metadata;
    const sourceMetrics = metadata && event.audioFrame
      ? sourceEndMetrics(
          metadata.sourceEndMs,
          event.audioFrame.binaryReceivedAtMs,
        )
      : {};
    const sourceEndBoundaryClientMs = (
      sourceMetrics.sourceEndBoundaryClientMs
    );

    eventsRef.current.push({
      stage: 'playback_chunk_scheduled',
      timestamp: event.schedulePerformanceMs - testStartRef.current,
      chunkIndex: receiveCountRef.current - 1,
      sourcePositionSec: 0,
      audioBytes: event.audioBytes,
      mediaDurationSec: event.sourceDurationSeconds,
      scheduledDurationSec: event.scheduledDurationSeconds,
      playbackWaitSec: event.waitBeforePlaybackSeconds,
      queueDepthSec: event.queueDepthSeconds,
      playbackRate: event.playbackRate,
      playbackMode: event.playbackMode,
      schedulePerformanceClientMs: event.schedulePerformanceMs,
      audioContextTimeAtScheduleSec: (
        event.audioContextTimeAtScheduleSeconds
      ),
      scheduledStartContextSec: event.scheduledStartContextSeconds,
      scheduledEndContextSec: event.scheduledEndContextSeconds,
      projectedScheduledStartClientMs: (
        event.projectedScheduledStartClientMs
      ),
      ...(metadata && event.audioFrame
        ? {
            audioMetadataProtocolVersion: metadata.protocolVersion,
            streamGeneration: metadata.streamGeneration,
            parentSequenceId: metadata.parentSequenceId,
            audioFrameId: metadata.audioFrameId,
            sourceStartMs: metadata.sourceStartMs,
            sourceEndMs: metadata.sourceEndMs,
            sourceTimingBasis: sourceTimingBasis(metadata),
            binaryReceiptClientMs: event.audioFrame.binaryReceivedAtMs,
            ...sourceMetrics,
            ...(sourceEndBoundaryClientMs === undefined
              ? {}
              : {
                  sourceEndToProjectedScheduledStartMs: (
                    event.projectedScheduledStartClientMs
                    - sourceEndBoundaryClientMs
                  ),
                }),
          }
        : {}),
    });
  }, [sourceEndMetrics]);

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
    () => sourcePositionRef.current,
    [],
  );
  const getCumulativeOutputDuration = useCallback(
    () => cumulativeOutputDurationRef.current,
    [],
  );

  return useMemo(() => ({
    startTest,
    logChunkSent,
    logAudioReceived,
    logAudioParentComplete,
    logPlaybackScheduled,
    logPlaybackQueueSample,
    getEvents,
    getSendCount,
    getReceiveCount,
    getSourcePosition,
    getCumulativeOutputDuration,
  }), [
    startTest,
    logChunkSent,
    logAudioReceived,
    logAudioParentComplete,
    logPlaybackScheduled,
    logPlaybackQueueSample,
    getEvents,
    getSendCount,
    getReceiveCount,
    getSourcePosition,
    getCumulativeOutputDuration,
  ]);
}
