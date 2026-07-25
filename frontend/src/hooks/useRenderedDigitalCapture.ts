import { useCallback, useEffect, useRef, useState } from 'react';
import type { PlaybackStartRouting } from './useAudioPlayback';
import {
  RENDERED_DIGITAL_CHANNEL_COUNT,
  RENDERED_DIGITAL_SAMPLE_RATE_HZ,
  type RenderedDigitalCaptureResult,
  type RenderedDigitalCaptureRouting,
  type RenderedDigitalPcmBlock,
  type RenderedDigitalSourceClock,
  type RenderedDigitalSourceClockParameters,
  type RenderedDigitalSourceClockTick,
} from '../types/renderedDigitalCapture';
import {
  countNonzeroSamples,
  deinterleaveStereoPcm16,
  encodeStereoPcm16Wav,
} from '../utils/renderedDigitalArtifacts';
import { sha256Hex } from '../utils/sha256';

const WORKLET_URL = '/rendered-digital-recorder.worklet.js';
const WORKLET_NAME = 'rendered-digital-recorder';
const WORKLET_BLOCK_FRAMES = 8000;
// The dashboard permits a 60-second fixture plus as much as 300 seconds of
// registered drain time. Keep a small teardown margin beyond that full window.
const MAXIMUM_CAPTURE_SECONDS = 420;
const CONTROL_TIMEOUT_MS = 5000;

interface RawRenderedDigitalPcmBlock {
  sequence: number;
  startContextFrame: number;
  frameCount: number;
  interleavedPcm16: Int16Array<ArrayBuffer>;
}

interface ActiveCaptureSession {
  context: AudioContext;
  recorderNode: AudioWorkletNode;
  sinkGain: GainNode;
  blocks: RawRenderedDigitalPcmBlock[];
  sourceTicks: RenderedDigitalSourceClockTick[];
  workletModuleSha256: string;
  sourceTickListeners: Set<
    (tick: RenderedDigitalSourceClockTick) => void
  >;
  captureStartContextFrame: number | null;
  captureEndContextFrameExclusive: number | null;
  stoppedBlockCount: number | null;
  contextStateViolationCount: number;
  visibilityViolationCount: number;
  fatalError: Error | null;
  startedResolve: (() => void) | null;
  startedReject: ((error: Error) => void) | null;
  stoppedResolve: (() => void) | null;
  stoppedReject: ((error: Error) => void) | null;
  sourceClockResolve: (() => void) | null;
  sourceClockReject: ((error: Error) => void) | null;
  removeLifecycleListeners: () => void;
}

export interface UseRenderedDigitalCaptureReturn {
  isCapturing: boolean;
  start: () => Promise<RenderedDigitalCaptureRouting>;
  createPlaybackRouting: (
    captureInputIndex: 0 | 1,
  ) => PlaybackStartRouting;
  getCurrentContextFrame: () => number | null;
  armSourceClock: (
    parameters: RenderedDigitalSourceClockParameters,
  ) => Promise<RenderedDigitalSourceClock>;
  stop: () => Promise<RenderedDigitalCaptureResult>;
  abort: () => Promise<void>;
}

function deferred<T = void>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: Error) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function withTimeout<T>(
  promise: Promise<T>,
  label: string,
): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(
      () => reject(new Error(`${label} timed out.`)),
      CONTROL_TIMEOUT_MS,
    );
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

function isSafeInteger(value: unknown, minimum = 0): value is number {
  return (
    typeof value === 'number'
    && Number.isSafeInteger(value)
    && value >= minimum
  );
}

function asMessage(value: unknown): Record<string, unknown> | null {
  return (
    typeof value === 'object'
    && value !== null
    && !Array.isArray(value)
  ) ? value as Record<string, unknown> : null;
}

function closeSessionNodes(session: ActiveCaptureSession): void {
  try {
    session.recorderNode.disconnect();
  } catch {
    // The context may already have detached the node.
  }
  try {
    session.sinkGain.disconnect();
  } catch {
    // The context may already have detached the node.
  }
  session.recorderNode.port.close();
  session.removeLifecycleListeners();
}

export function useRenderedDigitalCapture():
UseRenderedDigitalCaptureReturn {
  const [isCapturing, setIsCapturing] = useState(false);
  const activeSessionRef = useRef<ActiveCaptureSession | null>(null);
  const startInProgressRef = useRef(false);
  const pendingContextRef = useRef<AudioContext | null>(null);
  const mountedRef = useRef(true);

  const failSession = useCallback((
    session: ActiveCaptureSession,
    error: Error,
  ) => {
    if (session.fatalError === null) session.fatalError = error;
    session.startedReject?.(error);
    session.sourceClockReject?.(error);
    session.stoppedReject?.(error);
  }, []);

  const start = useCallback(async (): Promise<
  RenderedDigitalCaptureRouting
  > => {
    if (
      activeSessionRef.current !== null
      || startInProgressRef.current
    ) {
      throw new Error('A rendered-digital capture is already active.');
    }
    if (!globalThis.isSecureContext) {
      throw new Error(
        'Rendered-digital capture requires HTTPS or a localhost origin.',
      );
    }
    if (
      typeof AudioContext === 'undefined'
      || typeof AudioWorkletNode === 'undefined'
    ) {
      throw new Error('This browser does not support AudioWorklet capture.');
    }

    const context = new AudioContext({
      sampleRate: RENDERED_DIGITAL_SAMPLE_RATE_HZ,
      latencyHint: 'interactive',
    });
    pendingContextRef.current = context;
    startInProgressRef.current = true;
    try {
      if (context.sampleRate !== RENDERED_DIGITAL_SAMPLE_RATE_HZ) {
        throw new Error(
          'The browser did not create the required 16 kHz AudioContext.',
        );
      }
      if (context.state === 'suspended') {
        await withTimeout(context.resume(), 'AudioContext resume');
      }
      if (context.state !== 'running') {
        throw new Error(
          `AudioContext did not enter running state (${context.state}).`,
        );
      }
      const workletResponse = await withTimeout(
        fetch(WORKLET_URL, { cache: 'no-store' }),
        'Recorder worklet fetch',
      );
      if (!workletResponse.ok) {
        throw new Error(
          `Recorder worklet fetch returned HTTP ${workletResponse.status}.`,
        );
      }
      const workletBytes = await withTimeout(
        workletResponse.arrayBuffer(),
        'Recorder worklet bytes',
      );
      const workletModuleSha256 = await sha256Hex(workletBytes);
      const workletModuleUrl = URL.createObjectURL(
        new Blob([workletBytes], { type: 'text/javascript' }),
      );
      try {
        await withTimeout(
          context.audioWorklet.addModule(workletModuleUrl),
          'Recorder worklet load',
        );
      } finally {
        URL.revokeObjectURL(workletModuleUrl);
      }
      if (!mountedRef.current) {
        throw new Error('Rendered-digital capture was cancelled.');
      }

      const started = deferred();
      const recorderNode = new AudioWorkletNode(context, WORKLET_NAME, {
        numberOfInputs: RENDERED_DIGITAL_CHANNEL_COUNT,
        numberOfOutputs: 1,
        outputChannelCount: [RENDERED_DIGITAL_CHANNEL_COUNT],
        channelCount: 1,
        channelCountMode: 'explicit',
        channelInterpretation: 'discrete',
        processorOptions: {
          chunkFrames: WORKLET_BLOCK_FRAMES,
          maximumFrames: (
            RENDERED_DIGITAL_SAMPLE_RATE_HZ
            * MAXIMUM_CAPTURE_SECONDS
          ),
        },
      });
      const sinkGain = context.createGain();
      sinkGain.gain.value = 0;

      const session = {} as ActiveCaptureSession;
      const onStateChange = () => {
        if (
          activeSessionRef.current === session
          && context.state !== 'running'
        ) {
          session.contextStateViolationCount += 1;
        }
      };
      const onVisibilityChange = () => {
        if (
          activeSessionRef.current === session
          && document.visibilityState !== 'visible'
        ) {
          session.visibilityViolationCount += 1;
        }
      };
      context.addEventListener('statechange', onStateChange);
      document.addEventListener('visibilitychange', onVisibilityChange);

      Object.assign(session, {
        context,
        recorderNode,
        sinkGain,
        blocks: [],
        sourceTicks: [],
        workletModuleSha256,
        sourceTickListeners: new Set(),
        captureStartContextFrame: null,
        captureEndContextFrameExclusive: null,
        stoppedBlockCount: null,
        contextStateViolationCount: 0,
        visibilityViolationCount: (
          document.visibilityState === 'visible' ? 0 : 1
        ),
        fatalError: null,
        startedResolve: () => started.resolve(undefined),
        startedReject: started.reject,
        stoppedResolve: null,
        stoppedReject: null,
        sourceClockResolve: null,
        sourceClockReject: null,
        removeLifecycleListeners: () => {
          context.removeEventListener('statechange', onStateChange);
          document.removeEventListener(
            'visibilitychange',
            onVisibilityChange,
          );
        },
      });

      recorderNode.port.onmessage = (event: MessageEvent<unknown>) => {
        const message = asMessage(event.data);
        if (message === null || typeof message.type !== 'string') {
          failSession(
            session,
            new Error('Recorder emitted a malformed control message.'),
          );
          return;
        }
        if (message.type === 'started') {
          if (!isSafeInteger(message.captureStartContextFrame)) {
            failSession(
              session,
              new Error('Recorder emitted an invalid capture start frame.'),
            );
            return;
          }
          session.captureStartContextFrame = (
            message.captureStartContextFrame
          );
          session.startedResolve?.();
          session.startedResolve = null;
          session.startedReject = null;
          return;
        }
        if (message.type === 'pcm_block') {
          if (
            !isSafeInteger(message.sequence)
            || !isSafeInteger(message.startContextFrame)
            || !isSafeInteger(message.frameCount, 1)
            || !(message.interleavedPcm16 instanceof Int16Array)
            || message.interleavedPcm16.length
              !== message.frameCount * RENDERED_DIGITAL_CHANNEL_COUNT
          ) {
            failSession(
              session,
              new Error('Recorder emitted a malformed PCM block.'),
            );
            return;
          }
          session.blocks.push({
            sequence: message.sequence,
            startContextFrame: message.startContextFrame,
            frameCount: message.frameCount,
            interleavedPcm16: new Int16Array(
              message.interleavedPcm16,
            ),
          });
          return;
        }
        if (message.type === 'source_clock_armed') {
          session.sourceClockResolve?.();
          session.sourceClockResolve = null;
          session.sourceClockReject = null;
          return;
        }
        if (message.type === 'source_chunk_due') {
          if (
            !isSafeInteger(message.chunkIndex)
            || !isSafeInteger(message.sourceSampleStart)
            || !isSafeInteger(message.sourceSampleEndExclusive, 1)
            || !isSafeInteger(message.boundaryContextFrame)
            || !isSafeInteger(message.deliveredAfterContextFrame)
          ) {
            failSession(
              session,
              new Error('Recorder emitted a malformed source-clock tick.'),
            );
            return;
          }
          const receivedContextFrameBefore = Math.round(
            session.context.currentTime * session.context.sampleRate,
          );
          const receivedAtClientMs = performance.now();
          const receivedContextFrameAfter = Math.round(
            session.context.currentTime * session.context.sampleRate,
          );
          const tick: RenderedDigitalSourceClockTick = {
            chunkIndex: message.chunkIndex,
            sourceSampleStart: message.sourceSampleStart,
            sourceSampleEndExclusive: (
              message.sourceSampleEndExclusive
            ),
            boundaryContextFrame: message.boundaryContextFrame,
            deliveredAfterContextFrame: (
              message.deliveredAfterContextFrame
            ),
            receivedContextFrameBefore,
            receivedContextFrameAfter,
            receivedAtClientMs,
          };
          session.sourceTicks.push(tick);
          for (const listener of session.sourceTickListeners) listener(tick);
          return;
        }
        if (message.type === 'stopped') {
          if (
            !isSafeInteger(message.captureStartContextFrame)
            || !isSafeInteger(
              message.captureEndContextFrameExclusive,
              1,
            )
            || !isSafeInteger(message.blockCount, 1)
          ) {
            failSession(
              session,
              new Error('Recorder emitted an invalid stop acknowledgement.'),
            );
            return;
          }
          session.captureStartContextFrame = (
            message.captureStartContextFrame
          );
          session.captureEndContextFrameExclusive = (
            message.captureEndContextFrameExclusive
          );
          session.stoppedBlockCount = message.blockCount;
          session.stoppedResolve?.();
          session.stoppedResolve = null;
          session.stoppedReject = null;
          return;
        }
        if (message.type === 'capture_error') {
          const code = typeof message.code === 'string'
            ? message.code
            : 'unknown';
          failSession(
            session,
            new Error(`Recorder rejected the capture (${code}).`),
          );
        }
      };
      recorderNode.onprocessorerror = () => {
        failSession(
          session,
          new Error('The rendered-digital AudioWorklet processor failed.'),
        );
      };

      activeSessionRef.current = session;
      pendingContextRef.current = null;
      recorderNode.connect(sinkGain);
      sinkGain.connect(context.destination);
      await withTimeout(started.promise, 'Recorder startup');
      if (
        !mountedRef.current
        || activeSessionRef.current !== session
        || session.fatalError !== null
      ) {
        throw new Error('Rendered-digital capture was cancelled.');
      }
      startInProgressRef.current = false;
      if (pendingContextRef.current === context) {
        pendingContextRef.current = null;
      }
      setIsCapturing(true);
      return { audioContext: context, recorderNode };
    } catch (error) {
      startInProgressRef.current = false;
      if (pendingContextRef.current === context) {
        pendingContextRef.current = null;
      }
      const session = activeSessionRef.current;
      if (session?.context === context) {
        closeSessionNodes(session);
        activeSessionRef.current = null;
      }
      if (context.state !== 'closed') {
        try {
          await context.close();
        } catch {
          // Preserve the setup error when teardown races browser navigation.
        }
      }
      throw error;
    }
  }, [failSession]);

  const createPlaybackRouting = useCallback((
    captureInputIndex: 0 | 1,
  ): PlaybackStartRouting => {
    const session = activeSessionRef.current;
    if (session === null || session.fatalError !== null) {
      throw new Error('Rendered-digital capture is not ready for playback.');
    }
    return {
      audioContext: session.context,
      captureNode: session.recorderNode,
      captureInputIndex,
    };
  }, []);

  const getCurrentContextFrame = useCallback((): number | null => {
    const session = activeSessionRef.current;
    if (session === null || session.fatalError !== null) return null;
    return Math.round(
      session.context.currentTime * session.context.sampleRate,
    );
  }, []);

  const armSourceClock = useCallback(async (
    parameters: RenderedDigitalSourceClockParameters,
  ): Promise<RenderedDigitalSourceClock> => {
    const session = activeSessionRef.current;
    if (session === null || session.fatalError !== null) {
      throw new Error('Rendered-digital capture is not active.');
    }
    if (
      !isSafeInteger(parameters.sourceStartContextFrame)
      || !isSafeInteger(parameters.sourceFrameCount, 1)
      || !isSafeInteger(parameters.sourceChunkFrames, 1)
      || parameters.sourceFrameCount % parameters.sourceChunkFrames !== 0
    ) {
      throw new Error('Invalid rendered-digital source-clock request.');
    }
    const sourceStartContextFrame = parameters.sourceStartContextFrame;
    const sourceStartContextSeconds = (
      sourceStartContextFrame / session.context.sampleRate
    );
    const beforeClientMs = performance.now();
    const currentContextSeconds = session.context.currentTime;
    const afterClientMs = performance.now();
    const sourceZeroClientMs = (
      beforeClientMs + (afterClientMs - beforeClientMs) / 2
      + (
        sourceStartContextSeconds - currentContextSeconds
      ) * 1000
    );

    const armPromise = new Promise<void>((resolve, reject) => {
      session.sourceClockResolve = resolve;
      session.sourceClockReject = reject;
    });
    session.recorderNode.port.postMessage({
      type: 'arm_source_clock',
      sourceStartContextFrame,
      sourceFrameCount: parameters.sourceFrameCount,
      sourceChunkFrames: parameters.sourceChunkFrames,
    });
    await withTimeout(armPromise, 'Source-clock arm');

    return {
      sampleRateHz: session.context.sampleRate,
      sourceStartContextFrame,
      sourceFrameCount: parameters.sourceFrameCount,
      sourceChunkFrames: parameters.sourceChunkFrames,
      sourceZeroClientMs,
      getCurrentContextFrame: () => Math.round(
        session.context.currentTime * session.context.sampleRate,
      ),
      subscribe: (listener) => {
        for (const tick of session.sourceTicks) listener(tick);
        session.sourceTickListeners.add(listener);
        return () => session.sourceTickListeners.delete(listener);
      },
    };
  }, []);

  const stop = useCallback(async (): Promise<
  RenderedDigitalCaptureResult
  > => {
    const session = activeSessionRef.current;
    if (session === null) {
      throw new Error('No rendered-digital capture is active.');
    }
    if (session.fatalError !== null) throw session.fatalError;

    const stoppedPromise = new Promise<void>((resolve, reject) => {
      session.stoppedResolve = resolve;
      session.stoppedReject = reject;
    });
    session.recorderNode.port.postMessage({ type: 'stop' });
    await withTimeout(stoppedPromise, 'Recorder finalization');
    if (session.fatalError !== null) throw session.fatalError;

    activeSessionRef.current = null;
    setIsCapturing(false);
    closeSessionNodes(session);
    await session.context.close();

    const captureStart = session.captureStartContextFrame;
    const captureEnd = session.captureEndContextFrameExclusive;
    if (
      captureStart === null
      || captureEnd === null
      || captureEnd <= captureStart
      || session.stoppedBlockCount !== session.blocks.length
      || session.blocks.length === 0
    ) {
      throw new Error('Rendered-digital capture did not finalize completely.');
    }

    let expectedSequence = 0;
    let expectedContextFrame = captureStart;
    let totalFrames = 0;
    for (const block of session.blocks) {
      if (
        block.sequence !== expectedSequence
        || block.startContextFrame !== expectedContextFrame
      ) {
        throw new Error(
          'Rendered-digital PCM blocks contain a gap or overlap.',
        );
      }
      expectedSequence += 1;
      expectedContextFrame += block.frameCount;
      totalFrames += block.frameCount;
    }
    if (expectedContextFrame !== captureEnd) {
      throw new Error(
        'Rendered-digital PCM blocks do not tile the capture interval.',
      );
    }

    const interleavedPcm16 = new Int16Array(
      totalFrames * RENDERED_DIGITAL_CHANNEL_COUNT,
    );
    let sampleOffset = 0;
    const blocks: RenderedDigitalPcmBlock[] = [];
    for (const block of session.blocks) {
      interleavedPcm16.set(block.interleavedPcm16, sampleOffset);
      sampleOffset += block.interleavedPcm16.length;
      blocks.push({
        ...block,
        interleavedPcmSha256: await sha256Hex(
          block.interleavedPcm16.buffer,
        ),
      });
    }
    const { source, translated } = deinterleaveStereoPcm16(
      interleavedPcm16,
    );
    const wavBytes = encodeStereoPcm16Wav(
      interleavedPcm16,
      session.context.sampleRate,
    );

    return {
      sampleRateHz: session.context.sampleRate,
      channelCount: RENDERED_DIGITAL_CHANNEL_COUNT,
      captureStartContextFrame: captureStart,
      captureEndContextFrameExclusive: captureEnd,
      frameCount: totalFrames,
      blocks,
      interleavedPcm16,
      sourcePcm16: source,
      translatedPcm16: translated,
      wavBytes,
      interleavedPcmSha256: await sha256Hex(interleavedPcm16.buffer),
      sourcePcmSha256: await sha256Hex(source.buffer),
      translatedPcmSha256: await sha256Hex(translated.buffer),
      wavSha256: await sha256Hex(wavBytes),
      workletModuleSha256: session.workletModuleSha256,
      sourceNonzeroSampleCount: countNonzeroSamples(source),
      translatedNonzeroSampleCount: countNonzeroSamples(translated),
      contextStateViolationCount: session.contextStateViolationCount,
      visibilityViolationCount: session.visibilityViolationCount,
    };
  }, []);

  const abort = useCallback(async () => {
    const session = activeSessionRef.current;
    if (session === null) return;
    activeSessionRef.current = null;
    setIsCapturing(false);
    failSession(
      session,
      new Error('Rendered-digital capture was aborted.'),
    );
    try {
      session.recorderNode.port.postMessage({ type: 'abort' });
    } catch {
      // Continue local cleanup if the processor has already failed.
    }
    closeSessionNodes(session);
    if (session.context.state !== 'closed') {
      try {
        await session.context.close();
      } catch {
        // Abort is idempotent and best-effort. A browser-level close failure
        // must not prevent the dashboard from completing server teardown.
      }
    }
  }, [failSession]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      startInProgressRef.current = false;
      const pendingContext = pendingContextRef.current;
      pendingContextRef.current = null;
      if (pendingContext !== null && pendingContext.state !== 'closed') {
        void pendingContext.close().catch(() => {
          // Navigation cleanup cannot surface an asynchronous close failure.
        });
      }
      const session = activeSessionRef.current;
      if (session === null) return;
      activeSessionRef.current = null;
      failSession(
        session,
        new Error('Rendered-digital capture was cancelled by navigation.'),
      );
      try {
        session.recorderNode.port.postMessage({ type: 'abort' });
      } catch {
        // Continue teardown when navigation follows a processor failure.
      }
      closeSessionNodes(session);
      if (session.context.state !== 'closed') {
        void session.context.close().catch(() => {
          // Navigation cleanup cannot surface an asynchronous close failure.
        });
      }
    };
  }, [failSession]);

  return {
    isCapturing,
    start,
    createPlaybackRouting,
    getCurrentContextFrame,
    armSourceClock,
    stop,
    abort,
  };
}
