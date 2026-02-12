import { useCallback, useRef } from 'react';
import type { ClientTimingEvent } from '../types/timing';

interface UseTimingTrackerReturn {
  startTest: () => void;
  logChunkSent: (audioBytes: number) => void;
  logAudioReceived: (audioBytes: number) => void;
  getEvents: () => ClientTimingEvent[];
  getSendCount: () => number;
  getReceiveCount: () => number;
  getSourcePosition: () => number;
  getCumulativeOutputDuration: () => number;
}

const CHUNK_SIZE = 4800;
const SAMPLE_RATE = 16000;
const BYTES_PER_SAMPLE = 2; // Int16

export function useTimingTracker(): UseTimingTrackerReturn {
  const eventsRef = useRef<ClientTimingEvent[]>([]);
  const sendCountRef = useRef(0);
  const receiveCountRef = useRef(0);
  const cumulativeOutputSamplesRef = useRef(0);
  const testStartRef = useRef(0);

  const startTest = useCallback(() => {
    eventsRef.current = [];
    sendCountRef.current = 0;
    receiveCountRef.current = 0;
    cumulativeOutputSamplesRef.current = 0;
    testStartRef.current = performance.now();
  }, []);

  const logChunkSent = useCallback((audioBytes: number) => {
    const idx = sendCountRef.current;
    sendCountRef.current += 1;
    eventsRef.current.push({
      stage: 'chunk_sent',
      timestamp: performance.now() - testStartRef.current,
      chunkIndex: idx,
      sourcePositionSec: idx * (CHUNK_SIZE / SAMPLE_RATE),
      audioBytes,
    });
  }, []);

  const logAudioReceived = useCallback((audioBytes: number) => {
    const idx = receiveCountRef.current;
    receiveCountRef.current += 1;
    const samples = audioBytes / BYTES_PER_SAMPLE;
    cumulativeOutputSamplesRef.current += samples;
    eventsRef.current.push({
      stage: 'audio_received',
      timestamp: performance.now() - testStartRef.current,
      chunkIndex: idx,
      sourcePositionSec: 0, // not applicable for received audio
      audioBytes,
    });
  }, []);

  const getEvents = useCallback(() => [...eventsRef.current], []);
  const getSendCount = useCallback(() => sendCountRef.current, []);
  const getReceiveCount = useCallback(() => receiveCountRef.current, []);
  const getSourcePosition = useCallback(
    () => sendCountRef.current * (CHUNK_SIZE / SAMPLE_RATE),
    [],
  );
  const getCumulativeOutputDuration = useCallback(
    () => cumulativeOutputSamplesRef.current / SAMPLE_RATE,
    [],
  );

  return {
    startTest,
    logChunkSent,
    logAudioReceived,
    getEvents,
    getSendCount,
    getReceiveCount,
    getSourcePosition,
    getCumulativeOutputDuration,
  };
}
