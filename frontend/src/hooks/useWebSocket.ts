import { useCallback, useEffect, useRef, useState } from 'react';
import type { ServerMessage, ClientMessage, SessionStatus } from '../types/messages';
import {
  AUDIO_METADATA_PROTOCOL_VERSION,
  type AudioFrameObservation,
  type AudioMetadataProtocolVersion,
  type AudioParentCompleteObservation,
} from '../types/audioMetadata';
import { AudioMetadataProtocolV1Receiver } from '../utils/audioMetadataProtocol';

export interface UseWebSocketOptions {
  url: string;
  onStatus?: (status: SessionStatus, message: string) => void;
  onAudio?: (
    audio: ArrayBuffer,
    observation?: AudioFrameObservation,
  ) => void;
  onAudioParentComplete?: (
    observation: AudioParentCompleteObservation,
  ) => void;
  onLevel?: (rms: number) => void;
  onError?: (message: string) => void;
  reconnectInterval?: number;
  audioMetadataProtocolVersion?: AudioMetadataProtocolVersion;
}

export interface UseWebSocketReturn {
  isConnected: boolean;
  status: SessionStatus;
  sendMessage: (message: ClientMessage) => boolean;
  sendAudio: (audio: ArrayBuffer) => boolean;
  connect: () => void;
  disconnect: () => void;
}

export function useWebSocket({
  url,
  onStatus,
  onAudio,
  onAudioParentComplete,
  onLevel,
  onError,
  reconnectInterval = 3000,
  audioMetadataProtocolVersion,
}: UseWebSocketOptions): UseWebSocketReturn {
  const [isConnected, setIsConnected] = useState(false);
  const [status, setStatus] = useState<SessionStatus>('disconnected');

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  const shouldReconnectRef = useRef(false);
  const connectRef = useRef<() => void>(() => undefined);

  // Store callbacks in refs to avoid dependency issues
  const onStatusRef = useRef(onStatus);
  const onAudioRef = useRef(onAudio);
  const onAudioParentCompleteRef = useRef(onAudioParentComplete);
  const onLevelRef = useRef(onLevel);
  const onErrorRef = useRef(onError);
  const metadataProtocolRef = useRef<{
    receiver: AudioMetadataProtocolV1Receiver;
    errorReported: boolean;
  } | null>(null);

  // Update refs when callbacks change
  useEffect(() => {
    onStatusRef.current = onStatus;
    onAudioRef.current = onAudio;
    onAudioParentCompleteRef.current = onAudioParentComplete;
    onLevelRef.current = onLevel;
    onErrorRef.current = onError;
  }, [
    onStatus,
    onAudio,
    onAudioParentComplete,
    onLevel,
    onError,
  ]);

  const reportMetadataProtocolError = useCallback((error: unknown) => {
    const protocol = metadataProtocolRef.current;
    if (protocol?.errorReported) {
      return;
    }
    if (protocol) {
      protocol.errorReported = true;
    }
    const detail = error instanceof Error ? error.message : String(error);
    const message = `Audio metadata protocol error: ${detail}`;
    console.error(message);
    setStatus('error');
    onErrorRef.current?.(message);
  }, []);

  const finishMetadataProtocol = useCallback((
    context: string,
    detach: boolean = false,
  ): boolean => {
    const protocol = metadataProtocolRef.current;
    if (!protocol) {
      return true;
    }
    try {
      protocol.receiver.finishStream(context);
      return true;
    } catch (error) {
      reportMetadataProtocolError(error);
      return false;
    } finally {
      if (detach && metadataProtocolRef.current === protocol) {
        metadataProtocolRef.current = null;
      }
    }
  }, [reportMetadataProtocolError]);

  const connect = useCallback(() => {
    // Don't connect if already connected or connecting
    if (wsRef.current) {
      const state = wsRef.current.readyState;
      if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) {
        return;
      }
    }

    shouldReconnectRef.current = true;

    console.log('WebSocket connecting to:', url);
    const ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    const metadataProtocol = audioMetadataProtocolVersion
      === AUDIO_METADATA_PROTOCOL_VERSION
      ? {
          receiver: new AudioMetadataProtocolV1Receiver(),
          errorReported: false,
        }
      : null;
    let legacyMetadataFailed = false;
    let legacyMetadataErrorReported = false;
    metadataProtocolRef.current = metadataProtocol;

    ws.onopen = () => {
      console.log('WebSocket connected');
      setIsConnected(true);
      setStatus('connected');
    };

    ws.onclose = (event) => {
      console.log('WebSocket closed:', event.code, event.reason);
      if (metadataProtocolRef.current === metadataProtocol) {
        finishMetadataProtocol('WebSocket disconnect', true);
      }
      setIsConnected(false);
      setStatus('disconnected');
      wsRef.current = null;

      // Attempt to reconnect if we should
      if (shouldReconnectRef.current) {
        reconnectTimeoutRef.current = window.setTimeout(() => {
          connectRef.current();
        }, reconnectInterval);
      }
    };

    ws.onerror = (event) => {
      console.error('WebSocket error:', event);
      onErrorRef.current?.('WebSocket connection error');
    };

    ws.onmessage = (event) => {
      const messageReceivedAtMs = performance.now();

      // Binary data is translated audio
      if (event.data instanceof ArrayBuffer) {
        if (!metadataProtocol) {
          if (legacyMetadataFailed) {
            return;
          }
          onAudioRef.current?.(event.data);
          return;
        }

        try {
          const metadata = metadataProtocol.receiver.acceptBinary(event.data);
          onAudioRef.current?.(event.data, {
            metadata,
            binaryReceivedAtMs: messageReceivedAtMs,
          });
        } catch (error) {
          reportMetadataProtocolError(error);
        }
        return;
      }
      if (!metadataProtocol && legacyMetadataFailed) {
        return;
      }

      // Text data is JSON control message
      try {
        const parsedMessage: unknown = JSON.parse(event.data);
        if (
          typeof parsedMessage === 'object'
          && parsedMessage !== null
          && 'type' in parsedMessage
          && (
            parsedMessage.type === 'audio_frame'
            || parsedMessage.type === 'audio_parent_complete'
          )
        ) {
          if (!metadataProtocol) {
            legacyMetadataFailed = true;
            if (!legacyMetadataErrorReported) {
              legacyMetadataErrorReported = true;
              reportMetadataProtocolError(
                new Error(
                  'received opt-in audio metadata during a legacy stream',
                ),
              );
            }
            return;
          }

          const metadata = metadataProtocol.receiver.acceptMetadataMessage(
            parsedMessage,
          );
          if (metadata.type === 'audio_parent_complete') {
            onAudioParentCompleteRef.current?.({
              metadata,
              receivedAtMs: messageReceivedAtMs,
            });
          }
          return;
        }

        if (metadataProtocol) {
          metadataProtocol.receiver.observeControlMessage();
        }
        const message = parsedMessage as ServerMessage;

        switch (message.type) {
          case 'status': {
            const isTerminal = (
              message.status === 'completed'
              || message.status === 'stopped'
              || message.status === 'error'
            );
            if (
              metadataProtocol
              && isTerminal
              && !finishMetadataProtocol(`terminal status ${message.status}`)
            ) {
              // Do not expose a successful terminal state after strict
              // metadata reconciliation has failed.
              return;
            }
            setStatus(message.status);
            onStatusRef.current?.(message.status, message.message);
            break;
          }
          case 'error':
            setStatus('error');
            onErrorRef.current?.(message.message);
            if (metadataProtocol) {
              finishMetadataProtocol('terminal error');
            }
            break;
          case 'level':
            onLevelRef.current?.(message.rms);
            break;
          case 'pong':
            // Heartbeat response
            break;
          default:
            throw new Error('unsupported WebSocket control message type');
        }
      } catch (error) {
        if (metadataProtocol) {
          metadataProtocol.receiver.closeAfterProtocolViolation();
          reportMetadataProtocolError(error);
        } else {
          console.error('Failed to parse WebSocket message');
        }
      }
    };

    wsRef.current = ws;
  }, [
    url,
    reconnectInterval,
    audioMetadataProtocolVersion,
    finishMetadataProtocol,
    reportMetadataProtocolError,
  ]);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  const disconnect = useCallback(() => {
    shouldReconnectRef.current = false;

    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    if (wsRef.current) {
      finishMetadataProtocol('WebSocket disconnect', true);
      wsRef.current.close();
      wsRef.current = null;
    }

    setIsConnected(false);
    setStatus('disconnected');
  }, [finishMetadataProtocol]);

  const sendMessage = useCallback((message: ClientMessage) => {
    const socket = wsRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }

    let outboundMessage = message;
    if (
      message.type === 'start_stream'
      && audioMetadataProtocolVersion === AUDIO_METADATA_PROTOCOL_VERSION
    ) {
      const protocol = metadataProtocolRef.current;
      if (!protocol) {
        reportMetadataProtocolError(
          new Error('version 1 receiver is unavailable'),
        );
        return false;
      }
      protocol.errorReported = false;
      try {
        protocol.receiver.beginStream();
      } catch (error) {
        reportMetadataProtocolError(error);
        return false;
      }
      outboundMessage = {
        ...message,
        audioMetadataProtocolVersion: AUDIO_METADATA_PROTOCOL_VERSION,
      };
    }

    try {
      socket.send(JSON.stringify(outboundMessage));
      return true;
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      const transportMessage = (
        `WebSocket ${message.type} send failed: ${detail}`
      );
      console.error(transportMessage);
      setStatus('error');
      onErrorRef.current?.(transportMessage);
      return false;
    }
  }, [audioMetadataProtocolVersion, reportMetadataProtocolError]);

  const sendAudio = useCallback((audio: ArrayBuffer) => {
    const socket = wsRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      const message = 'Audio chunk was not sent because the WebSocket is not open.';
      console.error(message);
      setStatus('error');
      onErrorRef.current?.(message);
      return false;
    }

    try {
      socket.send(audio);
      return true;
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      const message = `Audio chunk send failed: ${detail}`;
      console.error(message);
      setStatus('error');
      onErrorRef.current?.(message);
      return false;
    }
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      shouldReconnectRef.current = false;
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (wsRef.current) {
        finishMetadataProtocol('WebSocket unmount', true);
        wsRef.current.close();
      }
    };
  }, [finishMetadataProtocol]);

  return {
    isConnected,
    status,
    sendMessage,
    sendAudio,
    connect,
    disconnect,
  };
}
