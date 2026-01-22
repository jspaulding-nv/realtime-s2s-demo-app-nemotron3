import { useCallback, useRef, useState } from 'react';

interface UseAudioCaptureOptions {
  sampleRate?: number;
  chunkSize?: number;
  onChunk: (chunk: ArrayBuffer) => void;
  onError?: (error: string) => void;
}

interface UseAudioCaptureReturn {
  isCapturing: boolean;
  startCapture: () => Promise<void>;
  stopCapture: () => void;
  audioLevel: number;
}

export function useAudioCapture({
  sampleRate = 16000,
  chunkSize = 4800,
  onChunk,
  onError,
}: UseAudioCaptureOptions): UseAudioCaptureReturn {
  const [isCapturing, setIsCapturing] = useState(false);
  const [audioLevel, setAudioLevel] = useState(0);

  const audioContextRef = useRef<AudioContext | null>(null);
  const workletNodeRef = useRef<AudioWorkletNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);

  const startCapture = useCallback(async () => {
    try {
      // Request microphone access
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: { ideal: sampleRate },
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
      streamRef.current = stream;

      // Create audio context
      const audioContext = new AudioContext({ sampleRate });
      audioContextRef.current = audioContext;

      // Load the audio worklet processor
      const workletCode = `
        class AudioProcessor extends AudioWorkletProcessor {
          constructor() {
            super();
            this.buffer = [];
            this.chunkSize = ${chunkSize};
          }

          process(inputs) {
            const input = inputs[0];
            if (input.length > 0) {
              const channelData = input[0];

              // Convert Float32 to Int16 and accumulate
              for (let i = 0; i < channelData.length; i++) {
                // Clamp to [-1, 1] and convert to Int16
                const sample = Math.max(-1, Math.min(1, channelData[i]));
                const int16Sample = sample < 0 ? sample * 32768 : sample * 32767;
                this.buffer.push(int16Sample);
              }

              // When we have enough samples, send a chunk
              while (this.buffer.length >= this.chunkSize) {
                const chunk = this.buffer.splice(0, this.chunkSize);
                const int16Array = new Int16Array(chunk);

                // Calculate RMS for visualization
                let sum = 0;
                for (let i = 0; i < chunk.length; i++) {
                  sum += chunk[i] * chunk[i];
                }
                const rms = Math.sqrt(sum / chunk.length) / 32767;

                this.port.postMessage({
                  type: 'chunk',
                  data: int16Array.buffer,
                  rms: rms,
                }, [int16Array.buffer]);
              }
            }
            return true;
          }
        }

        registerProcessor('audio-processor', AudioProcessor);
      `;

      const blob = new Blob([workletCode], { type: 'application/javascript' });
      const workletUrl = URL.createObjectURL(blob);

      await audioContext.audioWorklet.addModule(workletUrl);
      URL.revokeObjectURL(workletUrl);

      // Create worklet node
      const workletNode = new AudioWorkletNode(audioContext, 'audio-processor');
      workletNodeRef.current = workletNode;

      // Handle messages from the worklet
      workletNode.port.onmessage = (event) => {
        if (event.data.type === 'chunk') {
          onChunk(event.data.data);
          setAudioLevel(event.data.rms);
        }
      };

      // Connect microphone to worklet
      const source = audioContext.createMediaStreamSource(stream);
      source.connect(workletNode);
      // Don't connect to destination to avoid feedback

      setIsCapturing(true);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to access microphone';
      onError?.(message);
    }
  }, [sampleRate, chunkSize, onChunk, onError]);

  const stopCapture = useCallback(() => {
    // Stop the media stream
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    }

    // Disconnect and close the worklet
    if (workletNodeRef.current) {
      workletNodeRef.current.disconnect();
      workletNodeRef.current = null;
    }

    // Close the audio context
    if (audioContextRef.current) {
      audioContextRef.current.close();
      audioContextRef.current = null;
    }

    setIsCapturing(false);
    setAudioLevel(0);
  }, []);

  return {
    isCapturing,
    startCapture,
    stopCapture,
    audioLevel,
  };
}
