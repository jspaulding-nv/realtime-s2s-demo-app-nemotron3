// WebSocket message types

export type SessionStatus =
  | 'disconnected'
  | 'connected'
  | 'listening'
  | 'processing'
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
}

export interface StopStreamMessage {
  type: 'stop_stream';
}

export interface PingMessage {
  type: 'ping';
}

export type ClientMessage = StartStreamMessage | StopStreamMessage | PingMessage;

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
