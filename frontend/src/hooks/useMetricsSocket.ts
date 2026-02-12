import { useCallback, useEffect, useRef, useState } from 'react';
import type { BackendTimingEvent } from '../types/timing';

interface UseMetricsSocketReturn {
  events: BackendTimingEvent[];
  isConnected: boolean;
  connect: () => void;
  disconnect: () => void;
  clearEvents: () => void;
}

export function useMetricsSocket(): UseMetricsSocketReturn {
  const [events, setEvents] = useState<BackendTimingEvent[]>([]);
  const [isConnected, setIsConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const eventsRef = useRef<BackendTimingEvent[]>([]);
  const flushTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const connect = useCallback(() => {
    if (wsRef.current) return;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws/metrics`);

    ws.onopen = () => setIsConnected(true);

    ws.onclose = () => {
      setIsConnected(false);
      wsRef.current = null;
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as BackendTimingEvent;
        eventsRef.current.push(data);
      } catch {
        // ignore malformed messages
      }
    };

    wsRef.current = ws;

    // Batch state updates every 1 second for rendering performance
    flushTimerRef.current = setInterval(() => {
      if (eventsRef.current.length > 0) {
        setEvents([...eventsRef.current]);
      }
    }, 1000);
  }, []);

  const disconnect = useCallback(() => {
    if (flushTimerRef.current) {
      clearInterval(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setIsConnected(false);
  }, []);

  const clearEvents = useCallback(() => {
    eventsRef.current = [];
    setEvents([]);
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (flushTimerRef.current) clearInterval(flushTimerRef.current);
      if (wsRef.current) wsRef.current.close();
    };
  }, []);

  return { events, isConnected, connect, disconnect, clearEvents };
}
