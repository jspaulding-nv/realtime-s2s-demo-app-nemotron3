export interface FileAudioChunkObservation {
  chunkIndex: number;
  sampleRateHz: number;
  sourceSampleStart: number;
  sourceSampleEndExclusive: number;
  inputPcmSha256: string;
  inputPcmSampleCount: number;
  emittedAtMs: number;
  inputSampleZeroClientMs: number;
}

export type SourceTimingBasis =
  | 'attributed_range'
  | 'audio_processed/nonsemantic'
  | 'partial_range'
  | 'unavailable';

/** Client-side timing event recorded by the frontend. */
export interface ClientTimingEvent {
  stage: string;
  timestamp: number;
  chunkIndex: number;
  sourcePositionSec: number;
  audioBytes: number;
  mediaDurationSec?: number;
  scheduledDurationSec?: number;
  playbackWaitSec?: number;
  queueDepthSec?: number;
  playbackRate?: number;
  playbackMode?: string;
  adaptivePlaybackEnabled?: boolean;
  audioMetadataProtocolVersion?: number;
  streamGeneration?: number;
  parentSequenceId?: number;
  audioFrameId?: number;
  audioFrameCount?: number;
  sourceStartMs?: number | null;
  sourceEndMs?: number | null;
  sourceTimingBasis?: SourceTimingBasis;
  binaryReceiptClientMs?: number;
  parentCompleteReceivedClientMs?: number;
  inputSampleZeroClientMs?: number;
  inputChunkEmittedClientMs?: number;
  inputSourceSampleStart?: number;
  inputSourceSampleEndExclusive?: number;
  inputSampleRateHz?: number;
  inputPcmSha256?: string;
  inputPcmSampleCount?: number;
  inputLedgerValid?: boolean;
  sourceEndBoundaryClientMs?: number;
  sourceEndToBinaryReceiptMs?: number;
  sourceEndToParentCompleteMs?: number;
  schedulePerformanceClientMs?: number;
  audioContextTimeAtScheduleSec?: number;
  scheduledStartContextSec?: number;
  scheduledEndContextSec?: number;
  projectedScheduledStartClientMs?: number;
  sourceEndToProjectedScheduledStartMs?: number;
  playbackClockSessionId?: number;
  clockSampleSequence?: number;
  clockSampleReason?: string;
  clockSamplePerformanceClientMs?: number;
  clockSamplePerformanceBeforeClientMs?: number;
  clockSamplePerformanceAfterClientMs?: number;
  clockSampleContextSec?: number;
  clockSampleOutputContextSec?: number;
  clockSampleOutputPerformanceClientMs?: number;
  clockSampleBasis?: string;
  clockSampleQueueEndContextSec?: number;
}

/** Backend timing event received via /ws/metrics. */
export interface BackendTimingEvent {
  stage: string;
  timestamp: number;
  chunk_index: number;
  source_position_sec: number;
  audio_bytes_len: number;
  wall_clock: number;
}

/** A single data point for the drift chart. */
export interface DriftDataPoint {
  elapsedMinutes: number;
  driftSeconds: number;
}
