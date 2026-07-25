import { useCallback, useEffect, useRef, useState } from 'react';
import type { FileAudioChunkObservation } from '../types/timing';
import { extractExactMonoPcm16Wav } from '../utils/pcmWav';
import { sha256Hex } from '../utils/sha256';

export interface UseFileAudioSourceOptions {
  sampleRate?: number;
  chunkSize?: number;
  onChunk: (
    chunk: ArrayBuffer,
    observation: FileAudioChunkObservation,
  ) => void;
  onComplete?: () => void;
}

export interface UseFileAudioSourceReturn {
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

  const pcmBufferRef = useRef<Int16Array | null>(null);
  const inputPcmSha256Ref = useRef<string | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const chunkIndexRef = useRef(0);
  const isStreamingRef = useRef(false);
  const streamRunRef = useRef(0);
  const loadRunRef = useRef(0);

  const onChunkRef = useRef(onChunk);
  const onCompleteRef = useRef(onComplete);

  useEffect(() => {
    onChunkRef.current = onChunk;
    onCompleteRef.current = onComplete;
  }, [onChunk, onComplete]);

  const loadFile = useCallback(async (file: File) => {
    const loadId = loadRunRef.current + 1;
    loadRunRef.current = loadId;
    streamRunRef.current += 1;
    isStreamingRef.current = false;
    if (timeoutRef.current !== null) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    pcmBufferRef.current = null;
    inputPcmSha256Ref.current = null;
    chunkIndexRef.current = 0;
    setIsLoaded(false);
    setIsStreaming(false);
    setDuration(0);
    setPosition(0);

    const arrayBuffer = await file.arrayBuffer();
    const exactPcm = extractExactMonoPcm16Wav(arrayBuffer, sampleRate);
    let sourceSampleCount: number;
    let sourceDuration: number;
    let pcmBuffer: Int16Array<ArrayBuffer>;

    if (exactPcm !== null) {
      // Preserve the original little-endian PCM payload byte for byte. Copying
      // through a Uint8Array avoids a float conversion and does not depend on
      // the host's typed-array endianness.
      sourceSampleCount = exactPcm.sampleCount;
      sourceDuration = sourceSampleCount / sampleRate;
      const totalChunks = Math.ceil(sourceSampleCount / chunkSize);
      const totalPaddedSamples = totalChunks * chunkSize;
      pcmBuffer = new Int16Array(totalPaddedSamples);
      new Uint8Array(pcmBuffer.buffer).set(exactPcm.pcmBytes);
    } else {
      // Decode and resample formats that are not already the exact ASR wire
      // format. The second read is required because decodeAudioData may detach
      // the first ArrayBuffer.
      const tempCtx = new OfflineAudioContext(1, 1, sampleRate);
      const decoded = await tempCtx.decodeAudioData(arrayBuffer);
      const totalSamples = Math.round(decoded.duration * sampleRate);
      const offlineCtx = new OfflineAudioContext(1, totalSamples, sampleRate);
      const arrayBuffer2 = await file.arrayBuffer();
      const decoded2 = await offlineCtx.decodeAudioData(arrayBuffer2);
      const source = offlineCtx.createBufferSource();
      source.buffer = decoded2;
      source.connect(offlineCtx.destination);
      source.start();
      const resampled = await offlineCtx.startRendering();

      sourceSampleCount = resampled.length;
      sourceDuration = resampled.duration;
      const totalChunks = Math.ceil(sourceSampleCount / chunkSize);
      const totalPaddedSamples = totalChunks * chunkSize;
      pcmBuffer = new Int16Array(totalPaddedSamples);
      const sourceSamples = resampled.getChannelData(0);
      for (let i = 0; i < sourceSamples.length; i++) {
        const sample = Math.max(-1, Math.min(1, sourceSamples[i]));
        pcmBuffer[i] = sample < 0 ? sample * 32768 : sample * 32767;
      }
    }

    // The digest and every transmitted chunk come from the same immutable wire
    // image, including final-frame zero padding.
    const inputPcmSha256 = await sha256Hex(pcmBuffer.buffer);

    if (loadRunRef.current !== loadId) return;
    pcmBufferRef.current = pcmBuffer;
    inputPcmSha256Ref.current = inputPcmSha256;
    setDuration(sourceDuration);
    setPosition(0);
    chunkIndexRef.current = 0;
    setIsLoaded(true);
  }, [chunkSize, sampleRate]);

  const startStreaming = useCallback(() => {
    if (!pcmBufferRef.current || !inputPcmSha256Ref.current) return;

    const pcmBuffer = pcmBufferRef.current;
    const inputPcmSha256 = inputPcmSha256Ref.current;
    const totalPaddedSamples = pcmBuffer.length;
    const totalChunks = totalPaddedSamples / chunkSize;
    const inputSampleZeroClientMs = performance.now();
    const runId = streamRunRef.current + 1;

    streamRunRef.current = runId;
    if (timeoutRef.current !== null) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    chunkIndexRef.current = 0;
    isStreamingRef.current = true;
    setIsStreaming(true);
    setPosition(0);

    function isCurrentRun() {
      return (
        isStreamingRef.current
        && streamRunRef.current === runId
      );
    }

    function sendNext() {
      timeoutRef.current = null;
      if (!isCurrentRun()) return;

      const idx = chunkIndexRef.current;
      if (idx >= totalChunks) {
        isStreamingRef.current = false;
        setIsStreaming(false);
        onCompleteRef.current?.();
        return;
      }

      const start = idx * chunkSize;
      const end = start + chunkSize;
      // The transmitted frame is zero-padded to chunkSize, so its source-end
      // deadline follows the padded sample ledger rather than the file length.
      const sourceSampleEndExclusive = start + chunkSize;
      const sourceEndBoundaryClientMs = (
        inputSampleZeroClientMs
        + (sourceSampleEndExclusive / sampleRate) * 1000
      );
      const now = performance.now();

      // Schedule against the absolute source timeline. Re-check the deadline
      // in case a browser timer fires fractionally early.
      if (now < sourceEndBoundaryClientMs) {
        timeoutRef.current = setTimeout(
          sendNext,
          sourceEndBoundaryClientMs - now,
        );
        return;
      }

      // Copy the exact byte range from the already hashed wire image.
      const chunk = pcmBuffer.buffer.slice(
        start * Int16Array.BYTES_PER_ELEMENT,
        end * Int16Array.BYTES_PER_ELEMENT,
      ) as ArrayBuffer;

      const emittedAtMs = performance.now();
      onChunkRef.current(chunk, {
        chunkIndex: idx,
        sampleRateHz: sampleRate,
        sourceSampleStart: start,
        // The server processes the padded PCM frame, so this ledger follows
        // transmitted samples rather than only non-padding file samples.
        sourceSampleEndExclusive,
        inputPcmSha256,
        inputPcmSampleCount: totalPaddedSamples,
        emittedAtMs,
        inputSampleZeroClientMs,
      });

      if (!isCurrentRun()) return;

      chunkIndexRef.current = idx + 1;
      setPosition((idx + 1) * chunkSize / sampleRate);

      if (chunkIndexRef.current >= totalChunks) {
        // Completion belongs to the transmission of the final padded frame;
        // it does not require one more chunk interval or timer callback.
        isStreamingRef.current = false;
        setIsStreaming(false);
        onCompleteRef.current?.();
        return;
      }

      // Each call computes its next absolute source-end deadline, so late
      // callbacks do not permanently shift the rest of the stream.
      sendNext();
    }

    // The first chunk is sent at its source-end boundary (300 ms by default).
    sendNext();
  }, [chunkSize, sampleRate]);

  const stopStreaming = useCallback(() => {
    streamRunRef.current += 1;
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
