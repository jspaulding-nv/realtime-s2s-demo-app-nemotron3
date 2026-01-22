import { useCallback, useEffect, useRef, useState } from 'react';
import type { ServerMessage, ClientMessage, SessionStatus } from '../types/messages';

interface UseWebSocketOptions {
  url: string;
  onStatus?: (status: SessionStatus, message: string) => void;
  onAudio?: (audio: ArrayBuffer) => void;
  onLevel?: (rms: number) => void;
  onError?: (message: string) => void;
  reconnectInterval?: number;
}

interface UseWebSocketReturn {
  isConnected: boolean;
  status: SessionStatus;
  sendMessage: (message: ClientMessage) => void;
  sendAudio: (audio: ArrayBuffer) => void;
  connect: () => void;
  disconnect: () => void;
}

export function useWebSocket({
  url,
  onStatus,
  onAudio,
  onLevel,
  onError,
  reconnectInterval = 3000,
}: UseWebSocketOptions): UseWebSocketReturn {
  const [isConnected, setIsConnected] = useState(false);
  const [status, setStatus] = useState<SessionStatus>('disconnected');

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  const shouldReconnectRef = useRef(false);

  // Store callbacks in refs to avoid dependency issues
  const onStatusRef = useRef(onStatus);
  const onAudioRef = useRef(onAudio);
  const onLevelRef = useRef(onLevel);
  const onErrorRef = useRef(onError);

  // Update refs when callbacks change
  useEffect(() => {
    onStatusRef.current = onStatus;
    onAudioRef.current = onAudio;
    onLevelRef.current = onLevel;
    onErrorRef.current = onError;
  }, [onStatus, onAudio, onLevel, onError]);

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

    ws.onopen = () => {
      console.log('WebSocket connected');
      setIsConnected(true);
      setStatus('connected');
    };

    ws.onclose = (event) => {
      console.log('WebSocket closed:', event.code, event.reason);
      setIsConnected(false);
      setStatus('disconnected');
      wsRef.current = null;

      // Attempt to reconnect if we should
      if (shouldReconnectRef.current) {
        reconnectTimeoutRef.current = window.setTimeout(() => {
          connect();
        }, reconnectInterval);
      }
    };

    ws.onerror = (event) => {
      console.error('WebSocket error:', event);
      onErrorRef.current?.('WebSocket connection error');
    };

    ws.onmessage = (event) => {
      // Binary data is translated audio
      if (event.data instanceof ArrayBuffer) {
        console.log('WebSocket: received audio data, bytes:', event.data.byteLength);
        onAudioRef.current?.(event.data);
        return;
      }

      // Text data is JSON control message
      try {
        const message = JSON.parse(event.data) as ServerMessage;

        switch (message.type) {
          case 'status':
            setStatus(message.status);
            onStatusRef.current?.(message.status, message.message);
            break;
          case 'error':
            setStatus('error');
            onErrorRef.current?.(message.message);
            break;
          case 'level':
            onLevelRef.current?.(message.rms);
            break;
          case 'pong':
            // Heartbeat response
            break;
        }
      } catch {
        console.error('Failed to parse WebSocket message');
      }
    };

    wsRef.current = ws;
  }, [url, reconnectInterval]);

  const disconnect = useCallback(() => {
    shouldReconnectRef.current = false;

    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }

    setIsConnected(false);
    setStatus('disconnected');
  }, []);

  const sendMessage = useCallback((message: ClientMessage) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(message));
    }
  }, []);

  const sendAudio = useCallback((audio: ArrayBuffer) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(audio);
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
        wsRef.current.close();
      }
    };
  }, []);

  return {
    isConnected,
    status,
    sendMessage,
    sendAudio,
    connect,
    disconnect,
  };
}
