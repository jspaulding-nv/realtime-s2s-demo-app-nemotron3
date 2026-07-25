import { useCallback, useEffect, useReducer, useState } from 'react';
import { useWebSocket } from '../hooks/useWebSocket';
import { useAudioCapture } from '../hooks/useAudioCapture';
import {
  useAudioPlayback,
  type PlaybackMetrics,
} from '../hooks/useAudioPlayback';
import { StatusIndicator } from './StatusIndicator';
import { LanguageSelector } from './LanguageSelector';
import { AudioVisualizer } from './AudioVisualizer';
import { ControlButton } from './ControlButton';
import type { AppState, AppAction, Language, SessionStatus } from '../types/messages';
import {
  DEFAULT_PLAYBACK_POLICY,
  type PlaybackMode,
} from '../utils/playbackPolicy';

const AUDIO_CONFIG = {
  sampleRate: 16000,
  chunkSize: 4800,
};

const DEFAULT_LANGUAGES: Language[] = [
  { code: 'es-US', name: 'Spanish (US)', available: true },
];

interface PlaybackStatus {
  queueDepthSeconds: number;
  peakQueueDepthSeconds: number;
  playbackRate: number;
  playbackMode: PlaybackMode;
  aboveTarget: boolean;
  aboveLimit: boolean;
  limitExceededCount: number;
}

const INITIAL_PLAYBACK_STATUS: PlaybackStatus = {
  queueDepthSeconds: 0,
  peakQueueDepthSeconds: 0,
  playbackRate: DEFAULT_PLAYBACK_POLICY.normalRate,
  playbackMode: 'normal',
  aboveTarget: false,
  aboveLimit: false,
  limitExceededCount: 0,
};

function playbackStatusFromMetrics(metrics: PlaybackMetrics): PlaybackStatus {
  return {
    queueDepthSeconds: metrics.queueDepthSeconds,
    peakQueueDepthSeconds: metrics.peakQueueDepthSeconds,
    playbackRate: metrics.playbackRate,
    playbackMode: metrics.playbackMode,
    aboveTarget: metrics.aboveTarget,
    aboveLimit: metrics.aboveLimit,
    limitExceededCount: metrics.limitExceededCount,
  };
}

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
  const [hasPlaybackSession, setHasPlaybackSession] = useState(false);
  const [playbackStatus, setPlaybackStatus] = useState<PlaybackStatus>(
    INITIAL_PLAYBACK_STATUS,
  );
  // Audio playback hook
  const {
    queueAudio,
    start: startPlayback,
    stop: stopPlayback,
    getPlaybackMetrics,
  } = useAudioPlayback({
    sampleRate: AUDIO_CONFIG.sampleRate,
    adaptivePlayback: true,
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
    onAudio: (audio, observation) => {
      queueAudio(audio, observation);
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

  // The queue continues to decay while audio plays even when no new response
  // arrives. Sample it so the display does not remain stuck at an old value.
  useEffect(() => {
    if (!isTranslating) return;

    const updatePlaybackStatus = () => {
      setPlaybackStatus(playbackStatusFromMetrics(getPlaybackMetrics()));
    };
    updatePlaybackStatus();
    const timer = setInterval(updatePlaybackStatus, 500);
    return () => clearInterval(timer);
  }, [isTranslating, getPlaybackMetrics]);

  // Handle start/stop translation
  const handleToggle = useCallback(async () => {
    if (isTranslating) {
      // Stop translation
      const metrics = getPlaybackMetrics();
      // Preserve the final browser-side observation before stopPlayback closes
      // the AudioContext and discards any remaining scheduled audio.
      setPlaybackStatus(playbackStatusFromMetrics(metrics));
      stopCapture();
      stopPlayback();
      sendMessage({ type: 'stop_stream' });
      setIsTranslating(false);
    } else {
      // Start translation
      setPlaybackStatus(INITIAL_PLAYBACK_STATUS);
      setHasPlaybackSession(true);
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
    getPlaybackMetrics,
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

        {hasPlaybackSession && (
          <div
            aria-label="Spanish playback telemetry"
            data-session-active={isTranslating}
            data-queue-current-seconds={playbackStatus.queueDepthSeconds}
            data-queue-peak-seconds={playbackStatus.peakQueueDepthSeconds}
            data-playback-rate={playbackStatus.playbackRate}
            data-playback-mode={playbackStatus.playbackMode}
            data-limit-breaches={playbackStatus.limitExceededCount}
            className={`rounded-lg border p-3 mb-6 text-sm ${
              playbackStatus.aboveLimit
                ? 'bg-red-50 border-red-200 text-red-700'
                : playbackStatus.aboveTarget
                  ? 'bg-amber-50 border-amber-200 text-amber-700'
                  : 'bg-green-50 border-green-200 text-green-700'
            }`}
          >
            <div className="flex items-baseline justify-between gap-4">
              <span className="font-medium">Spanish playback telemetry</span>
              <span className="text-xs">
                {isTranslating ? 'Live browser queue' : 'Final stop snapshot'}
              </span>
            </div>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 mt-2 text-xs">
              <dt>{isTranslating ? 'Current queue' : 'Queue at stop'}</dt>
              <dd className="font-mono font-semibold text-right">
                {playbackStatus.queueDepthSeconds.toFixed(2)}s
              </dd>
              <dt>Peak queue</dt>
              <dd className="font-mono font-semibold text-right">
                {playbackStatus.peakQueueDepthSeconds.toFixed(2)}s
              </dd>
              <dt>Scheduled rate</dt>
              <dd className="font-mono font-semibold text-right">
                {playbackStatus.playbackRate.toFixed(2)}x ({playbackStatus.playbackMode})
              </dd>
              <dt>&gt;{DEFAULT_PLAYBACK_POLICY.limitQueueSeconds}s breaches</dt>
              <dd className="font-mono font-semibold text-right">
                {playbackStatus.limitExceededCount}
              </dd>
            </dl>
            <div className="text-xs mt-2">
              Audience target ≤{DEFAULT_PLAYBACK_POLICY.targetQueueSeconds}s;
              limit {DEFAULT_PLAYBACK_POLICY.limitQueueSeconds}s.
            </div>
            {playbackStatus.aboveLimit && (
              <p className="text-xs mt-1" role="alert">
                {isTranslating
                  ? 'The live queue is above the audience limit; all speech is still preserved.'
                  : 'The queue was above the audience limit when playback stopped.'}
              </p>
            )}
          </div>
        )}

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
