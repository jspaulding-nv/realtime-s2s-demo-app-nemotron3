import { useCallback, useRef, useState } from 'react';

interface UseFileAudioSourceOptions {
  sampleRate?: number;
  chunkSize?: number;
  onChunk: (chunk: ArrayBuffer) => void;
  onComplete?: () => void;
}

interface UseFileAudioSourceReturn {
  isLoaded: boolean;
  isStreaming: boolean;
  duration: number;
  position: number;
  loadFile: (file: File) => Promise<void>;
  startStreaming: () => void;
  stopStreaming: () => void;
}

export function useFileAudioSource({
  sampleRate = 16000,
  chunkSize = 4800,
  onChunk,
  onComplete,
}: UseFileAudioSourceOptions): UseFileAudioSourceReturn {
  const [isLoaded, setIsLoaded] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [duration, setDuration] = useState(0);
  const [position, setPosition] = useState(0);

  const audioBufferRef = useRef<AudioBuffer | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const chunkIndexRef = useRef(0);
  const isStreamingRef = useRef(false);

  const onChunkRef = useRef(onChunk);
  const onCompleteRef = useRef(onComplete);
  onChunkRef.current = onChunk;
  onCompleteRef.current = onComplete;

  const loadFile = useCallback(async (file: File) => {
    const arrayBuffer = await file.arrayBuffer();

    // Use OfflineAudioContext to decode and resample to target sample rate
    // We need to know the decoded length, so create a temporary decode first
    const tempCtx = new OfflineAudioContext(1, 1, sampleRate);
    const decoded = await tempCtx.decodeAudioData(arrayBuffer);

    // Now resample to target sample rate if needed
    const totalSamples = Math.round(decoded.duration * sampleRate);
    const offlineCtx = new OfflineAudioContext(1, totalSamples, sampleRate);
    // Need to re-decode since the buffer is detached after first decode
    const arrayBuffer2 = await file.arrayBuffer();
    const decoded2 = await offlineCtx.decodeAudioData(arrayBuffer2);
    const source = offlineCtx.createBufferSource();
    source.buffer = decoded2;
    source.connect(offlineCtx.destination);
    source.start();
    const resampled = await offlineCtx.startRendering();

    audioBufferRef.current = resampled;
    setDuration(resampled.duration);
    setPosition(0);
    chunkIndexRef.current = 0;
    setIsLoaded(true);
  }, [sampleRate]);

  const startStreaming = useCallback(() => {
    if (!audioBufferRef.current) return;

    const buffer = audioBufferRef.current;
    const channelData = buffer.getChannelData(0);
    const totalChunks = Math.ceil(channelData.length / chunkSize);

    chunkIndexRef.current = 0;
    isStreamingRef.current = true;
    setIsStreaming(true);
    setPosition(0);

    const chunkIntervalMs = (chunkSize / sampleRate) * 1000; // 300ms
    let expectedTime = performance.now() + chunkIntervalMs;

    function sendNext() {
      if (!isStreamingRef.current) return;

      const idx = chunkIndexRef.current;
      if (idx >= totalChunks) {
        // File finished
        isStreamingRef.current = false;
        setIsStreaming(false);
        onCompleteRef.current?.();
        return;
      }

      // Extract chunk and convert Float32 -> Int16
      const start = idx * chunkSize;
      const end = Math.min(start + chunkSize, channelData.length);
      const int16Array = new Int16Array(chunkSize);

      for (let i = 0; i < end - start; i++) {
        const sample = Math.max(-1, Math.min(1, channelData[start + i]));
        int16Array[i] = sample < 0 ? sample * 32768 : sample * 32767;
      }
      // Zero-pad if last chunk is shorter (already zeros from Int16Array constructor)

      onChunkRef.current(int16Array.buffer);

      chunkIndexRef.current = idx + 1;
      setPosition((idx + 1) * chunkSize / sampleRate);

      // Self-correcting timer
      const drift = performance.now() - expectedTime;
      expectedTime += chunkIntervalMs;
      timeoutRef.current = setTimeout(sendNext, Math.max(0, chunkIntervalMs - drift));
    }

    // Send first chunk immediately
    sendNext();
  }, [chunkSize, sampleRate]);

  const stopStreaming = useCallback(() => {
    isStreamingRef.current = false;
    setIsStreaming(false);
    if (timeoutRef.current !== null) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
  }, []);

  return {
    isLoaded,
    isStreaming,
    duration,
    position,
    loadFile,
    startStreaming,
    stopStreaming,
  };
}
