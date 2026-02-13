import { useCallback, useRef, useState } from 'react';

interface UseAudioPlaybackOptions {
  sampleRate?: number;
  initialMuted?: boolean;
}

interface UseAudioPlaybackReturn {
  isPlaying: boolean;
  isMuted: boolean;
  queueAudio: (audioData: ArrayBuffer) => void;
  start: () => void;
  stop: () => void;
  setMuted: (muted: boolean) => void;
  getPlaybackPosition: () => number;
}

export function useAudioPlayback({
  sampleRate = 16000,
  initialMuted = false,
}: UseAudioPlaybackOptions = {}): UseAudioPlaybackReturn {
  const [isPlaying, setIsPlaying] = useState(false);
  const [isMuted, setIsMutedState] = useState(initialMuted);

  const audioContextRef = useRef<AudioContext | null>(null);
  const nextStartTimeRef = useRef<number>(0);
  const isActiveRef = useRef<boolean>(false);
  const gainNodeRef = useRef<GainNode | null>(null);
  const playbackPositionRef = useRef(0);

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

    const gainNode = audioContextRef.current.createGain();
    gainNode.gain.value = initialMuted ? 0 : 1;
    gainNode.connect(audioContextRef.current.destination);
    gainNodeRef.current = gainNode;

    nextStartTimeRef.current = audioContextRef.current.currentTime;
    playbackPositionRef.current = 0;
    isActiveRef.current = true;
    setIsPlaying(true);
  }, [sampleRate, initialMuted]);

  const stop = useCallback(() => {
    console.log('AudioPlayback: stopping');
    isActiveRef.current = false;
    setIsPlaying(false);

    gainNodeRef.current = null;

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

      // Create buffer source and route through gain node
      const source = ctx.createBufferSource();
      source.buffer = audioBuffer;

      if (gainNodeRef.current) {
        source.connect(gainNodeRef.current);
      } else {
        source.connect(ctx.destination);
      }

      // Track playback position via onended
      const bufferDuration = audioBuffer.duration;
      source.onended = () => {
        playbackPositionRef.current += bufferDuration;
      };

      // Schedule playback
      const currentTime = ctx.currentTime;
      const startTime = Math.max(nextStartTimeRef.current, currentTime);

      console.log('AudioPlayback: scheduling at', startTime, 'duration:', audioBuffer.duration);
      source.start(startTime);
      nextStartTimeRef.current = startTime + audioBuffer.duration;
    },
    [sampleRate]
  );

  const setMuted = useCallback((muted: boolean) => {
    setIsMutedState(muted);
    if (gainNodeRef.current) {
      gainNodeRef.current.gain.value = muted ? 0 : 1;
    }
  }, []);

  const getPlaybackPosition = useCallback(() => playbackPositionRef.current, []);

  return {
    isPlaying,
    isMuted,
    queueAudio,
    start,
    stop,
    setMuted,
    getPlaybackPosition,
  };
}
