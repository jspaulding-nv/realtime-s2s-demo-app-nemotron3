// WebSocket message types
import type { AudioMetadataProtocolVersion } from './audioMetadata';

export type SessionStatus =
  | 'disconnected'
  | 'connected'
  | 'listening'
  | 'processing'
  | 'completed'
  | 'stopped'
  | 'error';

export interface StatusMessage {
  type: 'status';
  status: SessionStatus;
  message: string;
}

export interface ErrorMessage {
  type: 'error';
  message: string;
}

export interface LevelMessage {
  type: 'level';
  rms: number;
}

export interface PongMessage {
  type: 'pong';
}

export type ServerMessage = StatusMessage | ErrorMessage | LevelMessage | PongMessage;

// Client-to-server messages
export interface StartStreamMessage {
  type: 'start_stream';
  targetLanguage: string;
  audioMetadataProtocolVersion?: AudioMetadataProtocolVersion;
}

export interface StopStreamMessage {
  type: 'stop_stream';
}

export interface EndInputMessage {
  type: 'end_input';
}

export interface PingMessage {
  type: 'ping';
}

export type ClientMessage =
  | StartStreamMessage
  | EndInputMessage
  | StopStreamMessage
  | PingMessage;

// Language configuration
export interface Language {
  code: string;
  name: string;
  available: boolean;
}

// Audio configuration from backend
export interface AudioConfig {
  sampleRate: number;
  chunkSize: number;
  channels: number;
  pipelineMode?: 'monolithic' | 'staged';
  audioMetadataProtocolVersions?: AudioMetadataProtocolVersion[];
  repositoryProvenance?: {
    commit: string | null;
    dirty: boolean | null;
  };
  modelConfig?: {
    asr: {
      image: string;
      imageDigest: string | null;
      profile: string | null;
      eouMs: number;
      wordTimeOffsets: boolean;
      sourceLanguage: string;
    };
    nmt: {
      image: string;
      imageDigest: string | null;
      profile: string | null;
      model: string;
      sourceLanguage: string;
      targetLanguage: string;
    };
    tts: {
      image: string;
      imageDigest: string | null;
      profile: string | null;
      targetLanguage: string;
      voice: string | null;
    };
  };
  stagedConfig?: {
    telemetrySchemaVersion?: number;
    segmentMaxChars: number;
    segmentMaxAgeMs: number;
    asrEventQueueMaxSize: number;
    nmtQueueMaxSize: number;
    ttsQueueMaxSize: number;
    outputQueueMaxSize: number;
    nmtRpcTimeoutSeconds: number;
    ttsRpcTimeoutSeconds: number;
    ttsMaxSegmentAudioSeconds: number;
    ttsMaxRetries: number;
    ttsResponseChunkTelemetryEnabled: boolean;
    ttsPublisherHandoffTelemetryEnabled: boolean;
    ttsSubsegmentMaxChars: number;
    ttsSubsegmentMinChars: number;
    closeTimeoutSeconds: number;
    ttsIncrementalPublishEnabled?: boolean;
    ttsIncrementalFrameMs?: number;
    ttsIncrementalAtomicFallbackMaxChars?: number;
  };
}

// Application state
export interface AppState {
  status: SessionStatus;
  targetLanguage: string;
  audioLevel: number;
  errorMessage: string | null;
  isConnected: boolean;
}

export type AppAction =
  | { type: 'SET_STATUS'; status: SessionStatus; message?: string }
  | { type: 'SET_ERROR'; message: string }
  | { type: 'CLEAR_ERROR' }
  | { type: 'SET_LANGUAGE'; language: string }
  | { type: 'SET_AUDIO_LEVEL'; level: number }
  | { type: 'SET_CONNECTED'; connected: boolean };
