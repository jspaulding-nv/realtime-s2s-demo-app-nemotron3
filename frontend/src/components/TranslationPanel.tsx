import { useCallback, useEffect, useReducer, useState } from 'react';
import { useWebSocket } from '../hooks/useWebSocket';
import { useAudioCapture } from '../hooks/useAudioCapture';
import { useAudioPlayback } from '../hooks/useAudioPlayback';
import { StatusIndicator } from './StatusIndicator';
import { LanguageSelector } from './LanguageSelector';
import { AudioVisualizer } from './AudioVisualizer';
import { ControlButton } from './ControlButton';
import type { AppState, AppAction, Language, SessionStatus } from '../types/messages';

const AUDIO_CONFIG = {
  sampleRate: 16000,
  chunkSize: 4800,
};

const DEFAULT_LANGUAGES: Language[] = [
  { code: 'es-US', name: 'Spanish (US)', available: true },
];

const initialState: AppState = {
  status: 'disconnected',
  targetLanguage: 'es-US',
  audioLevel: 0,
  errorMessage: null,
  isConnected: false,
};

function reducer(state: AppState, action: AppAction): AppState {
  switch (action.type) {
    case 'SET_STATUS':
      return {
        ...state,
        status: action.status,
        errorMessage: null,
      };
    case 'SET_ERROR':
      return {
        ...state,
        status: 'error',
        errorMessage: action.message,
      };
    case 'CLEAR_ERROR':
      return {
        ...state,
        errorMessage: null,
      };
    case 'SET_LANGUAGE':
      return {
        ...state,
        targetLanguage: action.language,
      };
    case 'SET_AUDIO_LEVEL':
      return {
        ...state,
        audioLevel: action.level,
      };
    case 'SET_CONNECTED':
      return {
        ...state,
        isConnected: action.connected,
        status: action.connected ? 'connected' : 'disconnected',
      };
    default:
      return state;
  }
}

export function TranslationPanel() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [languages, setLanguages] = useState<Language[]>(DEFAULT_LANGUAGES);
  const [isTranslating, setIsTranslating] = useState(false);

  // Audio playback hook
  const { queueAudio, start: startPlayback, stop: stopPlayback } = useAudioPlayback({
    sampleRate: AUDIO_CONFIG.sampleRate,
  });

  // WebSocket hook
  const {
    isConnected,
    sendMessage,
    sendAudio,
    connect,
    disconnect,
  } = useWebSocket({
    url: `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws/translate`,
    onStatus: (status: SessionStatus, message: string) => {
      dispatch({ type: 'SET_STATUS', status, message });
    },
    onAudio: (audio: ArrayBuffer) => {
      queueAudio(audio);
    },
    onLevel: (rms: number) => {
      dispatch({ type: 'SET_AUDIO_LEVEL', level: rms });
    },
    onError: (message: string) => {
      dispatch({ type: 'SET_ERROR', message });
    },
  });

  // Audio capture hook
  const {
    startCapture,
    stopCapture,
    audioLevel: captureLevel,
  } = useAudioCapture({
    sampleRate: AUDIO_CONFIG.sampleRate,
    chunkSize: AUDIO_CONFIG.chunkSize,
    onChunk: (chunk: ArrayBuffer) => {
      sendAudio(chunk);
    },
    onError: (error: string) => {
      dispatch({ type: 'SET_ERROR', message: error });
    },
  });

  // Fetch languages on mount
  useEffect(() => {
    fetch('/api/languages')
      .then((res) => res.json())
      .then((data) => {
        if (data.languages) {
          setLanguages(data.languages);
        }
      })
      .catch(console.error);
  }, []);

  // Connect to WebSocket on mount
  useEffect(() => {
    connect();
    return () => disconnect();
  }, [connect, disconnect]);

  // Update connected state
  useEffect(() => {
    dispatch({ type: 'SET_CONNECTED', connected: isConnected });
  }, [isConnected]);

  // Handle start/stop translation
  const handleToggle = useCallback(async () => {
    if (isTranslating) {
      // Stop translation
      stopCapture();
      stopPlayback();
      sendMessage({ type: 'stop_stream' });
      setIsTranslating(false);
    } else {
      // Start translation
      startPlayback();
      sendMessage({ type: 'start_stream', targetLanguage: state.targetLanguage });
      await startCapture();
      setIsTranslating(true);
    }
  }, [
    isTranslating,
    startCapture,
    stopCapture,
    startPlayback,
    stopPlayback,
    sendMessage,
    state.targetLanguage,
  ]);

  // Handle language change
  const handleLanguageChange = useCallback(
    (language: string) => {
      dispatch({ type: 'SET_LANGUAGE', language });

      // If currently translating, restart with new language
      if (isTranslating) {
        sendMessage({ type: 'stop_stream' });
        setTimeout(() => {
          sendMessage({ type: 'start_stream', targetLanguage: language });
        }, 100);
      }
    },
    [isTranslating, sendMessage]
  );

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-50 to-gray-100 flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl shadow-xl p-8 w-full max-w-md">
        {/* Header */}
        <div className="text-center mb-8">
          <h1 className="text-2xl font-bold text-gray-800 mb-2">
            Real-Time Translation
          </h1>
          <p className="text-gray-500 text-sm">
            Speak English, hear it in another language
          </p>
        </div>

        {/* Status */}
        <div className="flex justify-center mb-6">
          <StatusIndicator status={state.status} />
        </div>

        {/* Audio Visualizer */}
        <div className="mb-6">
          <AudioVisualizer
            level={captureLevel || state.audioLevel}
            isActive={isTranslating}
          />
        </div>

        {/* Control Button */}
        <div className="flex justify-center mb-8">
          <ControlButton
            isActive={isTranslating}
            onClick={handleToggle}
            disabled={!isConnected}
          />
        </div>

        {/* Language Selector */}
        <div className="mb-6">
          <LanguageSelector
            languages={languages}
            selectedLanguage={state.targetLanguage}
            onChange={handleLanguageChange}
            disabled={!isConnected}
          />
        </div>

        {/* Error Message */}
        {state.errorMessage && (
          <div className="bg-red-50 border border-red-200 rounded-lg p-4 mb-4">
            <p className="text-red-700 text-sm">{state.errorMessage}</p>
            <button
              onClick={() => dispatch({ type: 'CLEAR_ERROR' })}
              className="text-red-500 text-xs mt-2 underline"
            >
              Dismiss
            </button>
          </div>
        )}

        {/* Instructions */}
        <div className="text-center text-gray-400 text-xs">
          <p>Click the microphone to start translating.</p>
          <p>Use headphones to prevent audio feedback.</p>
        </div>
      </div>
    </div>
  );
}
