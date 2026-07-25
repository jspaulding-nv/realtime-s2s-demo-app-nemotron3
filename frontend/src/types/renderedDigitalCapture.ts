export const RENDERED_DIGITAL_CAPTURE_SCHEMA =
  'rendered-digital-common-clock-preflight/v1' as const;
export const RENDERED_DIGITAL_BLOCK_LEDGER_SCHEMA =
  'rendered-digital-block-ledger/v1' as const;
export const RENDERED_DIGITAL_SAMPLE_RATE_HZ = 16000;
export const RENDERED_DIGITAL_CHANNEL_COUNT = 2;
export const RENDERED_DIGITAL_SOURCE_FRAMES = 960000;
export const RENDERED_DIGITAL_SOURCE_CHUNK_FRAMES = 4800;
export const RENDERED_DIGITAL_MAX_MAIN_THREAD_LAG_FRAMES = 1600;
export const RENDERED_DIGITAL_PREFLIGHT_FILE_SHA256 =
  '0c2cb04d9774f60472b55355f587da2148a053f3a55c05ff36c7dfc23be5c257';
export const RENDERED_DIGITAL_PREFLIGHT_PCM_SHA256 =
  '81720f2e23e5b85df4eb2be0bbd486b6591d0b1ed98580118e9e2e1e466bd51c';

export interface RenderedDigitalPcmBlock {
  sequence: number;
  startContextFrame: number;
  frameCount: number;
  interleavedPcm16: Int16Array<ArrayBuffer>;
  interleavedPcmSha256: string;
}

export interface RenderedDigitalCaptureResult {
  sampleRateHz: number;
  channelCount: 2;
  captureStartContextFrame: number;
  captureEndContextFrameExclusive: number;
  frameCount: number;
  blocks: RenderedDigitalPcmBlock[];
  interleavedPcm16: Int16Array<ArrayBuffer>;
  sourcePcm16: Int16Array<ArrayBuffer>;
  translatedPcm16: Int16Array<ArrayBuffer>;
  wavBytes: ArrayBuffer;
  interleavedPcmSha256: string;
  sourcePcmSha256: string;
  translatedPcmSha256: string;
  wavSha256: string;
  workletModuleSha256: string;
  sourceNonzeroSampleCount: number;
  translatedNonzeroSampleCount: number;
  contextStateViolationCount: number;
  visibilityViolationCount: number;
}

export interface RenderedDigitalSourceClockParameters {
  sourceStartContextFrame: number;
  sourceFrameCount: number;
  sourceChunkFrames: number;
}

export interface RenderedDigitalSourceClockTick {
  chunkIndex: number;
  sourceSampleStart: number;
  sourceSampleEndExclusive: number;
  boundaryContextFrame: number;
  deliveredAfterContextFrame: number;
  receivedContextFrameBefore: number;
  receivedContextFrameAfter: number;
  receivedAtClientMs: number;
}

export interface RenderedDigitalSourceClock {
  sampleRateHz: number;
  sourceStartContextFrame: number;
  sourceFrameCount: number;
  sourceChunkFrames: number;
  sourceZeroClientMs: number;
  getCurrentContextFrame: () => number;
  subscribe: (
    listener: (tick: RenderedDigitalSourceClockTick) => void,
  ) => () => void;
}

export interface RenderedDigitalCaptureRouting {
  audioContext: AudioContext;
  recorderNode: AudioWorkletNode;
}
