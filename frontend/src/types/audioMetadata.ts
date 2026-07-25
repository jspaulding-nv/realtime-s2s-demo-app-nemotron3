export const AUDIO_METADATA_PROTOCOL_VERSION = 1 as const;

export type AudioMetadataProtocolVersion =
  typeof AUDIO_METADATA_PROTOCOL_VERSION;

export interface AudioSourceRange {
  sourceStartMs: number | null;
  sourceEndMs: number | null;
}

export interface AudioFrameMetadata extends AudioSourceRange {
  type: 'audio_frame';
  protocolVersion: AudioMetadataProtocolVersion;
  streamGeneration: number;
  parentSequenceId: number;
  audioFrameId: number;
  audioBytes: number;
  sampleRateHz: number;
  channels: number;
  bytesPerSample: number;
}

export interface AudioParentCompleteMetadata extends AudioSourceRange {
  type: 'audio_parent_complete';
  protocolVersion: AudioMetadataProtocolVersion;
  streamGeneration: number;
  parentSequenceId: number;
  audioFrameCount: number;
  audioBytes: number;
}

export interface AudioFrameObservation {
  metadata: AudioFrameMetadata;
  binaryReceivedAtMs: number;
}

export interface AudioParentCompleteObservation {
  metadata: AudioParentCompleteMetadata;
  receivedAtMs: number;
}

export type AudioMetadataMessage =
  | AudioFrameMetadata
  | AudioParentCompleteMetadata;
