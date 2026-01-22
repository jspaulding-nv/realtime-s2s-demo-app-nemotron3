import { useCallback, useRef, useState } from 'react';

interface UseAudioPlaybackOptions {
  sampleRate?: number;
}

interface UseAudioPlaybackReturn {
  isPlaying: boolean;
  queueAudio: (audioData: ArrayBuffer) => void;
  start: () => void;
  stop: () => void;
}

export function useAudioPlayback({
  sampleRate = 16000,
}: UseAudioPlaybackOptions = {}): UseAudioPlaybackReturn {
  const [isPlaying, setIsPlaying] = useState(false);

  const audioContextRef = useRef<AudioContext | null>(null);
  const nextStartTimeRef = useRef<number>(0);
  const isActiveRef = useRef<boolean>(false);

  const start = useCallback(() => {
    console.log('AudioPlayback: starting');
    if (!audioContextRef.current) {
      audioContextRef.current = new AudioContext({ sampleRate });
      console.log('AudioPlayback: created AudioContext, state:', audioContextRef.current.state);
    }

    if (audioContextRef.current.state === 'suspended') {
      console.log('AudioPlayback: resuming suspended AudioContext');
      audioContextRef.current.resume();
    }

    nextStartTimeRef.current = audioContextRef.current.currentTime;
    isActiveRef.current = true;
    setIsPlaying(true);
  }, [sampleRate]);

  const stop = useCallback(() => {
    console.log('AudioPlayback: stopping');
    isActiveRef.current = false;
    setIsPlaying(false);

    if (audioContextRef.current) {
      audioContextRef.current.close();
      audioContextRef.current = null;
    }
  }, []);

  const queueAudio = useCallback(
    (audioData: ArrayBuffer) => {
      console.log('AudioPlayback: queueAudio called, bytes:', audioData.byteLength, 'active:', isActiveRef.current);

      if (!isActiveRef.current) {
        console.log('AudioPlayback: not active, ignoring');
        return;
      }

      if (!audioContextRef.current) {
        console.log('AudioPlayback: no AudioContext, ignoring');
        return;
      }

      const ctx = audioContextRef.current;
      console.log('AudioPlayback: AudioContext state:', ctx.state);

      // Convert Int16 PCM to Float32 for Web Audio API
      const int16Array = new Int16Array(audioData);
      const float32Array = new Float32Array(int16Array.length);

      for (let i = 0; i < int16Array.length; i++) {
        float32Array[i] = int16Array[i] / 32768.0;
      }

      console.log('AudioPlayback: converted', int16Array.length, 'samples');

      // Create audio buffer
      const audioBuffer = ctx.createBuffer(1, float32Array.length, sampleRate);
      audioBuffer.getChannelData(0).set(float32Array);

      // Create buffer source
      const source = ctx.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(ctx.destination);

      // Schedule playback
      const currentTime = ctx.currentTime;
      const startTime = Math.max(nextStartTimeRef.current, currentTime);

      console.log('AudioPlayback: scheduling at', startTime, 'duration:', audioBuffer.duration);
      source.start(startTime);
      nextStartTimeRef.current = startTime + audioBuffer.duration;
    },
    [sampleRate]
  );

  return {
    isPlaying,
    queueAudio,
    start,
    stop,
  };
}
