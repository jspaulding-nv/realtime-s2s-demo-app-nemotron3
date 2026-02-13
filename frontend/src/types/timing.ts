/** Client-side timing event recorded by the frontend. */
export interface ClientTimingEvent {
  stage: string;
  timestamp: number;
  chunkIndex: number;
  sourcePositionSec: number;
  audioBytes: number;
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
