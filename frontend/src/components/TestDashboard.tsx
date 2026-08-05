import { useCallback, useEffect, useRef, useState } from 'react';
import { useWebSocket } from '../hooks/useWebSocket';
import { useFileAudioSource } from '../hooks/useFileAudioSource';
import { useTimingTracker } from '../hooks/useTimingTracker';
import { useMetricsSocket } from '../hooks/useMetricsSocket';
import { useAudioPlayback } from '../hooks/useAudioPlayback';
import { useRenderedDigitalCapture } from '../hooks/useRenderedDigitalCapture';
import { DriftChart } from './DriftChart';
import {
  exportTimingDataAsCSV,
  serializeTimingDataAsCSV,
} from '../utils/csvExport';
import {
  downloadPrivateArtifact,
  serializeRenderedDigitalBlockLedger,
} from '../utils/renderedDigitalArtifacts';
import {
  buildRenderedDigitalManifest,
  type TranslatedTransportEvidence,
  type TranslatedTransportFrame,
} from '../utils/renderedDigitalManifest';
import type {
  DriftDataPoint,
  FileAudioChunkObservation,
} from '../types/timing';
import type { LoadedFilePcmSnapshot } from '../hooks/useFileAudioSource';
import type {
  PlaybackScheduleEvent,
} from '../hooks/useAudioPlayback';
import type {
  RenderedDigitalCaptureResult,
} from '../types/renderedDigitalCapture';
import {
  RENDERED_DIGITAL_SOURCE_CHUNK_FRAMES,
  RENDERED_DIGITAL_SOURCE_FRAMES,
} from '../types/renderedDigitalCapture';
import {
  DEFAULT_PLAYBACK_POLICY,
  summarizePlaybackQueue,
  type PlaybackQueueSample,
} from '../utils/playbackPolicy';
import {
  serializeTailFreshnessShadowEvidence,
  TailFreshnessShadowError,
  TailFreshnessShadowScheduler,
  type TailFreshnessShadowEvidence,
  type TailFreshnessShadowSummary,
} from '../utils/tailFreshnessShadow';
import type { AudioConfig, SessionStatus } from '../types/messages';

type TestPhase =
  | 'idle'
  | 'starting'
  | 'running'
  | 'draining'
  | 'completed'
  | 'failed';
type FinishedPhase = Extract<TestPhase, 'completed' | 'failed'>;
type ServerTerminalState = 'pending' | 'completed' | 'error';

const DRAIN_MIN_SEC = 10;
const DRAIN_IDLE_SEC = 5;
const DRAIN_MAX_SEC = 300;

const EMPTY_TAIL_SHADOW_SUMMARY: TailFreshnessShadowSummary = {
  framesReceived: 0,
  framesRetained: 0,
  framesDropped: 0,
  parentsReceived: 0,
  parentsTruncated: 0,
  parentsFullyDropped: 0,
  totalSourceDurationSeconds: 0,
  retainedSourceDurationSeconds: 0,
  droppedSourceDurationSeconds: 0,
  retainedSourcePercent: 0,
  lastDecisionQueueSeconds: 0,
  peakQueueBeforeTruncationSeconds: 0,
  peakQueueAfterTruncationSeconds: 0,
  truncationTriggerCount: 0,
  suppressedArrivalCount: 0,
  residualBreachEvents: 0,
  peakResidualOverCapSeconds: 0,
  maxDroppedParentSuffixSeconds: 0,
  hardCapAchieved: true,
  singleTailContractHolds: true,
};

export function TestDashboard() {
  const [phase, setPhase] = useState<TestPhase>('idle');
  const [failureMessage, setFailureMessage] = useState('');
  const [sourceChunksSent, setSourceChunksSent] = useState(0);
  const [
    serverTerminalState,
    setServerTerminalState,
  ] = useState<ServerTerminalState>('pending');
  const [adaptivePlaybackEnabled, setAdaptivePlaybackEnabled] = useState(true);
  const [tailFreshnessShadowEnabled, setTailFreshnessShadowEnabled] = (
    useState(false)
  );
  const [tailFreshnessShadowStatus, setTailFreshnessShadowStatus] = useState<
    'off' | 'ready' | 'running' | 'complete' | 'invalid'
  >('off');
  const [tailFreshnessShadowSummary, setTailFreshnessShadowSummary] = (
    useState<TailFreshnessShadowSummary>(EMPTY_TAIL_SHADOW_SUMMARY)
  );
  const [
    renderedDigitalCaptureEnabled,
    setRenderedDigitalCaptureEnabled,
  ] = useState(false);
  const [driftData, setDriftData] = useState<DriftDataPoint[]>([]);
  const [drainCountdown, setDrainCountdown] = useState(DRAIN_IDLE_SEC);
  const [stats, setStats] = useState({
    currentDrift: 0,
    avgDrift: 0,
    maxDrift: 0,
    elapsedSec: 0,
    chunksSent: 0,
    responsesReceived: 0,
    queueDepth: 0,
    queueP95: 0,
    peakQueueDepth: 0,
    playbackRate: 1,
    secondsAboveTarget: 0,
    secondsAboveLimit: 0,
    limitBreaches: 0,
  });

  const testStartTimeRef = useRef(0);
  const driftUpdateTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const fileStartTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastReceiveChangeRef = useRef(0);
  const drainStartTimeRef = useRef(0);
  const phaseRef = useRef<TestPhase>('idle');
  const serverTerminalStateRef = useRef<ServerTerminalState>('pending');
  const finishStartedRef = useRef(false);
  const startInProgressRef = useRef(false);
  const driftDataRef = useRef<DriftDataPoint[]>([]);
  const queueSamplesRef = useRef<PlaybackQueueSample[]>([]);
  const tailFreshnessShadowEnabledRef = useRef(false);
  const tailFreshnessShadowRef = useRef<
    TailFreshnessShadowScheduler | null
  >(null);
  const tailFreshnessShadowEvidenceRef = useRef<
    TailFreshnessShadowEvidence | null
  >(null);
  const tailFreshnessShadowErrorRef = useRef<string | null>(null);
  const renderedDigitalCaptureEnabledRef = useRef(false);
  const renderedDigitalCaptureResultRef = useRef<
  RenderedDigitalCaptureResult | null
  >(null);
  const renderedDigitalSourceSnapshotRef = useRef<
  LoadedFilePcmSnapshot | null
  >(null);
  const renderedDigitalSourceScheduleRef = useRef<
  PlaybackScheduleEvent | null
  >(null);
  const captureConfigRef = useRef<AudioConfig | null>(null);
  const translatedTransportEvidenceRef = useRef<
  TranslatedTransportEvidence
  >({
    received: [],
    scheduled: [],
  });

  const tracker = useTimingTracker();
  const trackerRef = useRef(tracker);
  const metrics = useMetricsSocket();
  const renderedDigitalCapture = useRenderedDigitalCapture();

  // Audio playback: input (English) starts muted, output (Spanish) starts muted
  const inputPlayback = useAudioPlayback({
    sampleRate: 16000,
    initialMuted: true,
    adaptivePlayback: false,
    minimumScheduleLeadSeconds: (
      renderedDigitalCaptureEnabled ? 0.25 : 0
    ),
    quantizeScheduleToSampleFrames: renderedDigitalCaptureEnabled,
  });
  const outputPlayback = useAudioPlayback({
    sampleRate: 16000,
    initialMuted: true,
    adaptivePlayback: adaptivePlaybackEnabled,
    onSchedule: (event) => {
      trackerRef.current.logPlaybackScheduled(event);
      const shadow = tailFreshnessShadowRef.current;
      if (!tailFreshnessShadowEnabledRef.current || shadow === null) return;
      try {
        if (!event.audioFrame) {
          throw new TailFreshnessShadowError(
            'scheduled PCM lacks protocol-v1 frame metadata',
          );
        }
        shadow.observeFrame({
          observation: event.audioFrame,
          schedulePerformanceMs: event.schedulePerformanceMs,
          audioContextTimeAtScheduleSeconds: (
            event.audioContextTimeAtScheduleSeconds
          ),
          audioBytes: event.audioBytes,
          sourceDurationSeconds: event.sourceDurationSeconds,
        });
      } catch (error) {
        const message = error instanceof Error
          ? error.message
          : 'tail-freshness shadow evidence failed';
        tailFreshnessShadowErrorRef.current = message;
        setTailFreshnessShadowStatus('invalid');
      }
    },
    onClockSample: (event) => (
      trackerRef.current.logPlaybackClockSample(event)
    ),
  });

  // Stable refs for playback instances
  const inputPlaybackRef = useRef(inputPlayback);
  const outputPlaybackRef = useRef(outputPlayback);
  const renderedDigitalCaptureRef = useRef(renderedDigitalCapture);

  useEffect(() => {
    trackerRef.current = tracker;
    inputPlaybackRef.current = inputPlayback;
    outputPlaybackRef.current = outputPlayback;
    renderedDigitalCaptureRef.current = renderedDigitalCapture;
  }, [
    tracker,
    inputPlayback,
    outputPlayback,
    renderedDigitalCapture,
  ]);

  // Keep refs in sync with state
  useEffect(() => {
    phaseRef.current = phase;
    console.log('[TestDashboard] Phase changed to:', phase);
  }, [phase]);
  useEffect(() => {
    renderedDigitalCaptureEnabledRef.current = (
      renderedDigitalCaptureEnabled
    );
  }, [renderedDigitalCaptureEnabled]);
  useEffect(() => {
    tailFreshnessShadowEnabledRef.current = tailFreshnessShadowEnabled;
    if (phaseRef.current === 'idle') {
      setTailFreshnessShadowStatus(
        tailFreshnessShadowEnabled ? 'ready' : 'off',
      );
    }
  }, [tailFreshnessShadowEnabled]);
  useEffect(() => { driftDataRef.current = driftData; }, [driftData]);

  // Store stable function refs to avoid closure issues
  const wsRef = useRef<ReturnType<typeof useWebSocket>>(null!);
  const fileSourceRef = useRef<ReturnType<typeof useFileAudioSource>>(null!);
  const finishTestRef = useRef<(
    finishedPhase?: FinishedPhase,
    message?: string,
  ) => Promise<void>>(null!);

  // Chunk counter for logging (doesn't need to trigger re-renders)
  const chunkLogCountRef = useRef(0);
  const audioLogCountRef = useRef(0);

  // WebSocket for audio transport
  const ws = useWebSocket({
    url: `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws/translate`,
    audioMetadataProtocolVersion: 1,
    onStatus: (status: SessionStatus, message: string) => {
      if (
        status === 'completed'
        && serverTerminalStateRef.current === 'pending'
      ) {
        trackerRef.current.logServerTerminal('completed');
        serverTerminalStateRef.current = 'completed';
        setServerTerminalState('completed');
      } else if (
        status === 'error'
        && serverTerminalStateRef.current === 'pending'
      ) {
        trackerRef.current.logServerTerminal('error');
        serverTerminalStateRef.current = 'error';
        setServerTerminalState('error');
        if (phaseRef.current === 'running' || phaseRef.current === 'draining') {
          fileSourceRef.current?.stopStreaming();
          void finishTestRef.current?.(
            'failed',
            message || 'Riva reported a translation error.',
          );
        }
      }
    },
    onError: (message: string) => {
      if (serverTerminalStateRef.current !== 'pending') return;
      trackerRef.current.logServerTerminal('error');
      serverTerminalStateRef.current = 'error';
      setServerTerminalState('error');
      if (phaseRef.current === 'running' || phaseRef.current === 'draining') {
        fileSourceRef.current?.stopStreaming();
        void finishTestRef.current?.('failed', message);
      }
    },
    onAudio: (audio, observation) => {
      audioLogCountRef.current += 1;
      if (audioLogCountRef.current <= 5 || audioLogCountRef.current % 50 === 0) {
        console.log(`[TestDashboard] onAudio #${audioLogCountRef.current}: ${audio.byteLength} bytes`);
      }
      let receivedEvidence: TranslatedTransportFrame | null = null;
      if (renderedDigitalCaptureEnabledRef.current) {
        if (!observation) {
          const message = (
            'Rendered-digital capture received PCM without frame metadata.'
          );
          serverTerminalStateRef.current = 'error';
          setServerTerminalState('error');
          fileSourceRef.current?.stopStreaming();
          void finishTestRef.current?.('failed', message);
          return;
        }
        const metadata = observation.metadata;
        receivedEvidence = {
          sequence: translatedTransportEvidenceRef.current.received.length,
          streamGeneration: metadata.streamGeneration,
          parentSequenceId: metadata.parentSequenceId,
          audioFrameId: metadata.audioFrameId,
          sampleRateHz: metadata.sampleRateHz,
          channels: metadata.channels,
          bytesPerSample: metadata.bytesPerSample,
          // Receipt and schedule evidence use independent private copies.
          pcm: audio.slice(0),
        };
        translatedTransportEvidenceRef.current.received.push(
          receivedEvidence,
        );
      }
      trackerRef.current.logAudioReceived(audio.byteLength, observation);
      lastReceiveChangeRef.current = performance.now();
      // Queue translated audio for output playback
      const scheduleEvent = outputPlaybackRef.current.queueAudio(
        audio,
        observation,
      );
      if (renderedDigitalCaptureEnabledRef.current) {
        if (scheduleEvent === null || receivedEvidence === null) {
          const message = (
            'Rendered-digital capture could not schedule received PCM.'
          );
          serverTerminalStateRef.current = 'error';
          setServerTerminalState('error');
          fileSourceRef.current?.stopStreaming();
          void finishTestRef.current?.('failed', message);
          return;
        }
        translatedTransportEvidenceRef.current.scheduled.push({
          ...receivedEvidence,
          sequence: (
            translatedTransportEvidenceRef.current.scheduled.length
          ),
          pcm: audio.slice(0),
        });
      }
    },
    onAudioParentComplete: (observation) => {
      trackerRef.current.logAudioParentComplete(observation);
      const shadow = tailFreshnessShadowRef.current;
      if (tailFreshnessShadowEnabledRef.current && shadow !== null) {
        try {
          shadow.observeParentComplete(observation);
        } catch (error) {
          const message = error instanceof Error
            ? error.message
            : 'tail-freshness parent evidence failed';
          tailFreshnessShadowErrorRef.current = message;
          setTailFreshnessShadowStatus('invalid');
        }
      }
      lastReceiveChangeRef.current = performance.now();
    },
  });

  useEffect(() => {
    wsRef.current = ws;
  }, [ws]);

  // File audio source
  const fileSource = useFileAudioSource({
    onChunk: (chunk, observation) => {
      const sent = wsRef.current?.sendAudio(chunk) ?? false;
      if (!sent) {
        if (serverTerminalStateRef.current === 'pending') {
          const message = 'Audio capture stopped because a chunk could not be sent.';
          serverTerminalStateRef.current = 'error';
          setServerTerminalState('error');
          fileSourceRef.current?.stopStreaming();
          if (phaseRef.current === 'running' || phaseRef.current === 'draining') {
            void finishTestRef.current?.('failed', message);
          }
        }
        return;
      }

      const postSendContextFrame = renderedDigitalCaptureEnabledRef.current
        ? renderedDigitalCaptureRef.current.getCurrentContextFrame()
        : undefined;
      if (
        renderedDigitalCaptureEnabledRef.current
        && postSendContextFrame === null
      ) {
        const message = (
          'The post-WebSocket AudioContext send frame was unavailable.'
        );
        serverTerminalStateRef.current = 'error';
        setServerTerminalState('error');
        fileSourceRef.current?.stopStreaming();
        if (phaseRef.current === 'running' || phaseRef.current === 'draining') {
          void finishTestRef.current?.('failed', message);
        }
        return;
      }
      const completedObservation: FileAudioChunkObservation = {
        ...observation,
        // These samples are deliberately taken only after sendAudio confirms
        // that the complete PCM frame was handed to the WebSocket API.
        emittedAtMs: performance.now(),
        ...(typeof postSendContextFrame === 'number'
          ? { inputChunkEmittedContextFrame: postSendContextFrame }
          : {}),
      };

      chunkLogCountRef.current += 1;
      if (chunkLogCountRef.current <= 5 || chunkLogCountRef.current % 50 === 0) {
        console.log(`[TestDashboard] onChunk #${chunkLogCountRef.current}: ${chunk.byteLength} bytes`);
      }
      trackerRef.current.logChunkSent(
        chunk.byteLength,
        completedObservation,
      );
      setSourceChunksSent((count) => count + 1);
      // Formal rendered-digital mode schedules the exact padded source once
      // and uses its AudioWorklet boundaries to pace these sends. The normal
      // dashboard retains its historical per-chunk monitor path.
      if (!renderedDigitalCaptureEnabledRef.current) {
        inputPlaybackRef.current.queueAudio(chunk);
      }
    },
    onComplete: () => {
      console.log('[TestDashboard] onComplete fired. phaseRef.current =', phaseRef.current);
      if (phaseRef.current === 'running') {
        // Close the request-audio side while leaving the WebSocket open for
        // final Riva responses and the browser playback queue to drain.
        if (!wsRef.current.sendMessage({ type: 'end_input' })) {
          const message = 'The end-of-input control message could not be sent.';
          serverTerminalStateRef.current = 'error';
          setServerTerminalState('error');
          void finishTestRef.current?.('failed', message);
          return;
        }
        trackerRef.current.logInputEnded();
        lastReceiveChangeRef.current = performance.now();
        drainStartTimeRef.current = performance.now();
        setPhase('draining');
      }
    },
    onError: (message) => {
      if (serverTerminalStateRef.current === 'pending') {
        serverTerminalStateRef.current = 'error';
        setServerTerminalState('error');
      }
      if (phaseRef.current === 'running' || phaseRef.current === 'draining') {
        void finishTestRef.current?.('failed', message);
      }
    },
  });

  useEffect(() => {
    fileSourceRef.current = fileSource;
  }, [fileSource]);

  // -- Queue sampling plus the historical cross-language duration comparison --
  const updateDriftData = useCallback(() => {
    const t = trackerRef.current;
    const elapsedMs = performance.now() - testStartTimeRef.current;
    const elapsedSec = elapsedMs / 1000;
    const sendCount = t.getSendCount();
    const recvCount = t.getReceiveCount();

    // Legacy duration drift compares unlike English and Spanish media
    // positions. Keep it for continuity; the browser queue below is the
    // listener-backlog metric.
    const inputPosition = fileSourceRef.current.position;
    const playbackPosition = outputPlaybackRef.current.getPlaybackPosition();
    const drift = inputPosition - playbackPosition;
    const playbackMetrics = outputPlaybackRef.current.getPlaybackMetrics();
    const queueSample: PlaybackQueueSample = {
      timestampSeconds: elapsedSec,
      queueDepthSeconds: playbackMetrics.queueDepthSeconds,
      playbackRate: playbackMetrics.playbackRate,
    };
    queueSamplesRef.current.push(queueSample);
    const queueSummary = summarizePlaybackQueue(queueSamplesRef.current);
    if (
      tailFreshnessShadowEnabledRef.current
      && tailFreshnessShadowRef.current !== null
      && tailFreshnessShadowErrorRef.current === null
    ) {
      try {
        setTailFreshnessShadowSummary(
          tailFreshnessShadowRef.current.getSummary(),
        );
      } catch (error) {
        tailFreshnessShadowErrorRef.current = error instanceof Error
          ? error.message
          : 'tail-freshness shadow summary failed';
        setTailFreshnessShadowStatus('invalid');
      }
    }
    trackerRef.current.logPlaybackQueueSample(
      playbackMetrics.queueDepthSeconds,
      playbackMetrics.playbackRate,
      playbackMetrics.playbackMode,
    );

    // Log every 5 seconds
    if (Math.floor(elapsedSec) % 5 === 0) {
      console.log(`[TestDashboard] updateDriftData: elapsed=${elapsedSec.toFixed(1)}s, sent=${sendCount}, recv=${recvCount}, inputPos=${inputPosition.toFixed(2)}s, playbackPos=${playbackPosition.toFixed(2)}s, drift=${drift.toFixed(2)}s`);
    }

    const point: DriftDataPoint = {
      elapsedMinutes: elapsedSec / 60,
      driftSeconds: drift,
    };

    setDriftData((prev) => [...prev, point]);

    const allDrifts = [...driftDataRef.current, point];
    setStats({
      currentDrift: drift,
      avgDrift:
        allDrifts.length > 0
          ? allDrifts.reduce((s, d) => s + d.driftSeconds, 0) / allDrifts.length
          : 0,
      maxDrift:
        allDrifts.length > 0
          ? Math.max(...allDrifts.map((d) => d.driftSeconds))
          : 0,
      elapsedSec,
      chunksSent: sendCount,
      responsesReceived: recvCount,
      queueDepth: playbackMetrics.queueDepthSeconds,
      queueP95: queueSummary.p95QueueDepthSeconds,
      peakQueueDepth: playbackMetrics.peakQueueDepthSeconds,
      playbackRate: playbackMetrics.playbackRate,
      secondsAboveTarget: queueSummary.secondsAboveTarget,
      secondsAboveLimit: queueSummary.secondsAboveLimit,
      limitBreaches: playbackMetrics.limitExceededCount,
    });
  }, []);

  // -- Start test --
  const handleStart = useCallback(async () => {
    console.log('[TestDashboard] handleStart called');
    if (startInProgressRef.current || phaseRef.current !== 'idle') return;
    startInProgressRef.current = true;
    setPhase('starting');
    if (fileStartTimerRef.current !== null) {
      clearTimeout(fileStartTimerRef.current);
      fileStartTimerRef.current = null;
    }
    setDriftData([]);
    driftDataRef.current = [];
    queueSamplesRef.current = [];
    testStartTimeRef.current = performance.now();
    lastReceiveChangeRef.current = performance.now();
    chunkLogCountRef.current = 0;
    setSourceChunksSent(0);
    audioLogCountRef.current = 0;
    serverTerminalStateRef.current = 'pending';
    setServerTerminalState('pending');
    finishStartedRef.current = false;
    setFailureMessage('');
    setDrainCountdown(DRAIN_IDLE_SEC);
    renderedDigitalCaptureResultRef.current = null;
    renderedDigitalSourceSnapshotRef.current = null;
    renderedDigitalSourceScheduleRef.current = null;
    captureConfigRef.current = null;
    translatedTransportEvidenceRef.current = {
      received: [],
      scheduled: [],
    };
    tailFreshnessShadowRef.current = null;
    tailFreshnessShadowEvidenceRef.current = null;
    tailFreshnessShadowErrorRef.current = null;
    setTailFreshnessShadowSummary(EMPTY_TAIL_SHADOW_SUMMARY);
    setTailFreshnessShadowStatus(
      tailFreshnessShadowEnabledRef.current ? 'ready' : 'off',
    );

    if (
      renderedDigitalCaptureEnabled
      && Math.abs(fileSourceRef.current.duration - 60) > 0.01
    ) {
      setFailureMessage(
        'Rendered-digital mode accepts only the exact 60-second preflight.',
      );
      startInProgressRef.current = false;
      setPhase('idle');
      return;
    }

    console.log('[TestDashboard] Checking /api/config capabilities...');
    let backendTestStarted = false;
    try {
      // AudioContext creation stays inside the click activation window. The
      // mode is default-off, so ordinary dashboard runs do not load a worklet
      // or retain raw rendered audio.
      if (renderedDigitalCaptureEnabled) {
        await renderedDigitalCaptureRef.current.start();
      }

      const configResponse = await fetch('/api/config');
      if (!configResponse.ok) {
        throw new Error(
          `Could not discover audio metadata capabilities (HTTP ${configResponse.status}).`,
        );
      }
      const config = await configResponse.json() as AudioConfig;
      if (!config.audioMetadataProtocolVersions?.includes(1)) {
        throw new Error(
          'Audio metadata protocol version 1 is unavailable. '
          + 'Use the staged schema-3 incremental-TTS pipeline.',
        );
      }
      captureConfigRef.current = config;

      console.log('[TestDashboard] Calling /api/test/start...');
      const response = await fetch('/api/test/start', { method: 'POST' });
      if (!response.ok) {
        let detail = `Backend returned HTTP ${response.status}.`;
        try {
          const payload = await response.json() as { detail?: string };
          if (payload.detail) detail = payload.detail;
        } catch {
          // Preserve the HTTP fallback when the response is not JSON.
        }
        throw new Error(detail);
      }
      backendTestStarted = true;

      metrics.connect();
      metrics.clearEvents();
      trackerRef.current.startTest({ adaptivePlaybackEnabled });

      if (tailFreshnessShadowEnabled) {
        const shadow = new TailFreshnessShadowScheduler({
          hardCapSeconds: DEFAULT_PLAYBACK_POLICY.limitQueueSeconds,
          cancellationGuardSeconds: 0.1,
          adaptivePlayback: adaptivePlaybackEnabled,
          playbackPolicy: DEFAULT_PLAYBACK_POLICY,
        });
        shadow.begin();
        tailFreshnessShadowRef.current = shadow;
        setTailFreshnessShadowStatus('running');
      }

      if (renderedDigitalCaptureEnabled) {
        inputPlaybackRef.current.start(
          renderedDigitalCaptureRef.current.createPlaybackRouting(0),
        );
        outputPlaybackRef.current.start(
          renderedDigitalCaptureRef.current.createPlaybackRouting(1),
        );
      } else {
        inputPlaybackRef.current.start();
        outputPlaybackRef.current.start();
      }
    } catch (error) {
      inputPlaybackRef.current.stop();
      outputPlaybackRef.current.stop();
      try {
        await renderedDigitalCaptureRef.current.abort();
      } catch {
        // Preserve the original setup failure if teardown also fails.
      }
      if (backendTestStarted) {
        try {
          await fetch('/api/test/stop', { method: 'POST' });
        } catch {
          // Preserve the original setup failure.
        }
      }
      setFailureMessage(
        error instanceof Error ? error.message : 'Backend test setup failed.',
      );
      startInProgressRef.current = false;
      setPhase('idle');
      return;
    }
    console.log('[TestDashboard] /api/test/start returned');

    console.log('[TestDashboard] Calling ws.connect()...');
    wsRef.current.connect();
    startInProgressRef.current = false;
    setPhase('running');
  }, [
    metrics,
    adaptivePlaybackEnabled,
    renderedDigitalCaptureEnabled,
    tailFreshnessShadowEnabled,
  ]);

  // -- Once WS connects, start stream + file source --
  const hasStartedStreamRef = useRef(false);
  useEffect(() => {
    if (phase === 'running' && ws.isConnected && !hasStartedStreamRef.current) {
      hasStartedStreamRef.current = true;
      console.log('[TestDashboard] WS connected during running phase, sending start_stream');
      wsRef.current.sendMessage({ type: 'start_stream', targetLanguage: 'es-US' });
      console.log('[TestDashboard] Will start file streaming in 500ms');
      fileStartTimerRef.current = setTimeout(() => {
        fileStartTimerRef.current = null;
        if (
          phaseRef.current !== 'running'
          || serverTerminalStateRef.current !== 'pending'
        ) {
          return;
        }
        console.log('[TestDashboard] Starting file streaming now');
        if (!renderedDigitalCaptureEnabledRef.current) {
          fileSourceRef.current.startStreaming();
          return;
        }
        void (async () => {
          const snapshot = (
            fileSourceRef.current.getLoadedPcmSnapshot?.() ?? null
          );
          if (
            snapshot === null
            || snapshot.sampleRateHz !== 16000
            || snapshot.sampleCount !== RENDERED_DIGITAL_SOURCE_FRAMES
            || snapshot.sampleCount
              % RENDERED_DIGITAL_SOURCE_CHUNK_FRAMES !== 0
          ) {
            throw new Error(
              'The selected file is not the exact 60-second PCM preflight.',
            );
          }
          const schedule = inputPlaybackRef.current.queueAudio(snapshot.pcm);
          if (schedule === null) {
            throw new Error('The common-clock source could not be scheduled.');
          }
          renderedDigitalSourceSnapshotRef.current = snapshot;
          renderedDigitalSourceScheduleRef.current = schedule;
          const sourceClock = await (
            renderedDigitalCaptureRef.current.armSourceClock({
              sourceStartContextFrame: (
                schedule.scheduledStartContextFrameFloor
              ),
              sourceFrameCount: snapshot.sampleCount,
              sourceChunkFrames: RENDERED_DIGITAL_SOURCE_CHUNK_FRAMES,
            })
          );
          if (
            phaseRef.current !== 'running'
            || serverTerminalStateRef.current !== 'pending'
          ) {
            return;
          }
          fileSourceRef.current.startStreaming(sourceClock);
        })().catch((error: unknown) => {
          const message = error instanceof Error
            ? error.message
            : 'Common-clock source setup failed.';
          serverTerminalStateRef.current = 'error';
          setServerTerminalState('error');
          void finishTestRef.current?.('failed', message);
        });
      }, 500);
    }
    if (phase !== 'running' && phase !== 'draining') {
      hasStartedStreamRef.current = false;
    }
    return () => {
      if (fileStartTimerRef.current !== null) {
        clearTimeout(fileStartTimerRef.current);
        fileStartTimerRef.current = null;
      }
    };
  }, [phase, ws.isConnected]);

  // -- Periodic drift updates during running + draining --
  useEffect(() => {
    if (phase === 'running' || phase === 'draining') {
      console.log('[TestDashboard] Starting drift update interval (phase=' + phase + ')');
      driftUpdateTimerRef.current = setInterval(updateDriftData, 1000);
      return () => {
        console.log('[TestDashboard] Clearing drift update interval');
        if (driftUpdateTimerRef.current) clearInterval(driftUpdateTimerRef.current);
      };
    }
  }, [phase, updateDriftData]);

  // -- Finish: disconnect everything and preserve either terminal outcome --
  const finishTest = useCallback(async (
    finishedPhase: FinishedPhase = 'completed',
    message = '',
  ) => {
    if (finishStartedRef.current) return;
    finishStartedRef.current = true;
    if (driftUpdateTimerRef.current) {
      clearInterval(driftUpdateTimerRef.current);
      driftUpdateTimerRef.current = null;
    }
    let terminalPhase = finishedPhase;
    let terminalMessage = message;
    console.log('[TestDashboard] finishTest called');
    if (fileStartTimerRef.current !== null) {
      clearTimeout(fileStartTimerRef.current);
      fileStartTimerRef.current = null;
    }
    const runCleanupStep = (label: string, step: () => void) => {
      try {
        step();
      } catch (error) {
        console.error(`[TestDashboard] ${label} failed during cleanup:`, error);
      }
    };
    runCleanupStep(
      'stop_stream control',
      () => { wsRef.current.sendMessage({ type: 'stop_stream' }); },
    );
    runCleanupStep('WebSocket disconnect', () => wsRef.current.disconnect());
    runCleanupStep('metrics disconnect', () => metrics.disconnect());
    if (tailFreshnessShadowEnabledRef.current) {
      const shadow = tailFreshnessShadowRef.current;
      const priorError = tailFreshnessShadowErrorRef.current;
      if (shadow === null || priorError !== null) {
        terminalPhase = 'failed';
        terminalMessage = priorError
          ? `Tail-freshness shadow evidence failed: ${priorError}`
          : 'Tail-freshness shadow evidence was not initialized.';
        setTailFreshnessShadowStatus('invalid');
      } else {
        try {
          const evidence = shadow.finish();
          tailFreshnessShadowEvidenceRef.current = evidence;
          setTailFreshnessShadowSummary(evidence.summary);
          setTailFreshnessShadowStatus('complete');
        } catch (error) {
          terminalPhase = 'failed';
          terminalMessage = error instanceof Error
            ? `Tail-freshness shadow evidence failed: ${error.message}`
            : 'Tail-freshness shadow evidence could not be finalized.';
          tailFreshnessShadowErrorRef.current = terminalMessage;
          setTailFreshnessShadowStatus('invalid');
        }
      }
    }
    runCleanupStep('input playback stop', () => inputPlaybackRef.current.stop());
    runCleanupStep('output playback stop', () => outputPlaybackRef.current.stop());

    if (renderedDigitalCaptureEnabledRef.current) {
      try {
        renderedDigitalCaptureResultRef.current = await (
          renderedDigitalCaptureRef.current.stop()
        );
      } catch (error) {
        try {
          await renderedDigitalCaptureRef.current.abort();
        } catch {
          // Capture teardown is best-effort; the failed terminal state and
          // backend stop request below must still be completed.
        }
        terminalPhase = 'failed';
        terminalMessage = error instanceof Error
          ? error.message
          : 'Rendered-digital capture could not be finalized.';
      }
    }

    try {
      await fetch('/api/test/stop', { method: 'POST' });
    } catch (error) {
      console.error('[TestDashboard] Failed to stop timing capture:', error);
    } finally {
      if (driftUpdateTimerRef.current) {
        clearInterval(driftUpdateTimerRef.current);
        driftUpdateTimerRef.current = null;
      }
      setFailureMessage(
        terminalPhase === 'failed' ? terminalMessage : '',
      );
      setPhase(terminalPhase);
    }
  }, [metrics]);

  useEffect(() => {
    finishTestRef.current = finishTest;
  }, [finishTest]);

  useEffect(() => {
    const fatalError = renderedDigitalCapture.fatalError;
    if (
      fatalError === null
      || !renderedDigitalCaptureEnabledRef.current
      || (phase !== 'running' && phase !== 'draining')
    ) {
      return;
    }
    fileSourceRef.current?.stopStreaming();
    if (serverTerminalStateRef.current === 'pending') {
      serverTerminalStateRef.current = 'error';
      setServerTerminalState('error');
    }
    void finishTestRef.current?.('failed', fatalError.message);
  }, [phase, renderedDigitalCapture.fatalError]);

  // -- Draining: require the server terminal event, network quiet, and an empty queue --
  useEffect(() => {
    if (phase !== 'draining') return;

    console.log(
      '[TestDashboard] Draining phase started; waiting for server completion, network, and playback queue',
    );
    const checkInterval = setInterval(() => {
      const drainElapsedSec = (
        performance.now() - drainStartTimeRef.current
      ) / 1000;
      const silenceSec = (performance.now() - lastReceiveChangeRef.current) / 1000;
      const queueDepthSec = outputPlaybackRef.current.getPlaybackMetrics()
        .queueDepthSeconds;
      const remaining = Math.max(0, Math.ceil(DRAIN_IDLE_SEC - silenceSec));
      console.log(
        `[TestDashboard] Drain check: elapsed=${drainElapsedSec.toFixed(1)}s, `
        + `silence=${silenceSec.toFixed(1)}s, queue=${queueDepthSec.toFixed(1)}s`,
      );
      setDrainCountdown(remaining);

      const fullyDrained =
        serverTerminalStateRef.current === 'completed'
        && drainElapsedSec >= DRAIN_MIN_SEC
        && silenceSec >= DRAIN_IDLE_SEC
        // getPlaybackMetrics clamps a truly empty queue to exact zero. Do not
        // stop up to 100 ms early: the formal clock trace needs the sampler to
        // observe currentTime reaching the scheduled endpoint and emit the
        // matching queue_drained boundary.
        && queueDepthSec === 0;
      const timedOut = drainElapsedSec >= DRAIN_MAX_SEC;

      if (fullyDrained) {
        console.log('[TestDashboard] Server completed and playback queue drained');
        clearInterval(checkInterval);
        finishTestRef.current?.();
      } else if (timedOut) {
        const message = serverTerminalStateRef.current === 'completed'
          ? 'Timed out waiting for the translated playback queue to drain.'
          : 'Timed out waiting for Riva to confirm translation completion.';
        console.error(`[TestDashboard] ${message}`);
        clearInterval(checkInterval);
        finishTestRef.current?.('failed', message);
      }
    }, 1000);

    return () => clearInterval(checkInterval);
  }, [phase]);

  // -- Manual stop --
  const handleStop = useCallback(async () => {
    console.log('[TestDashboard] handleStop called');
    fileSourceRef.current.stopStreaming();
    await finishTestRef.current?.(
      renderedDigitalCaptureEnabledRef.current ? 'failed' : 'completed',
      renderedDigitalCaptureEnabledRef.current
        ? 'Rendered-digital preflight was stopped before completion.'
        : '',
    );
  }, []);

  // -- Evidence export --
  const handleExport = useCallback(async () => {
    console.log('[TestDashboard] handleExport called');
    const clientEvents = trackerRef.current.getEvents();
    let backendEvents = [];
    try {
      const resp = await fetch('/api/test/export');
      if (!resp.ok) throw new Error(`Export returned HTTP ${resp.status}.`);
      const data = await resp.json();
      backendEvents = data.events || [];
    } catch (err) {
      console.error('[TestDashboard] Export failed:', err);
    }
    console.log(
      `[TestDashboard] Export: ${clientEvents.length} client events, `
      + `${backendEvents.length} backend events`,
    );

    if (!renderedDigitalCaptureEnabledRef.current) {
      exportTimingDataAsCSV(clientEvents, backendEvents);
      if (tailFreshnessShadowEnabledRef.current) {
        const evidence = tailFreshnessShadowEvidenceRef.current;
        if (!evidence) {
          setFailureMessage(
            'Evidence export failed: tail-freshness shadow result is missing.',
          );
          return;
        }
        const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
        downloadPrivateArtifact(
          serializeTailFreshnessShadowEvidence(evidence),
          'application/json',
          `tail-freshness-shadow-${timestamp}.json`,
        );
      }
      return;
    }
    try {
      const capture = renderedDigitalCaptureResultRef.current;
      const sourceSnapshot = renderedDigitalSourceSnapshotRef.current;
      const sourceSchedule = renderedDigitalSourceScheduleRef.current;
      const config = captureConfigRef.current;
      if (!capture || !sourceSnapshot || !sourceSchedule || !config) {
        throw new Error(
          'Rendered-digital evidence is incomplete and cannot be exported.',
        );
      }
      if (phaseRef.current !== 'completed') {
        throw new Error(
          'Only a normally completed preflight can produce a formal bundle.',
        );
      }
      const timingCsv = serializeTimingDataAsCSV(
        clientEvents,
        backendEvents,
      );
      const blockLedgerCsv = serializeRenderedDigitalBlockLedger(
        capture.blocks,
      );
      const manifest = await buildRenderedDigitalManifest({
        capture,
        sourceSnapshot,
        sourceSchedule,
        config,
        clientEvents,
        adaptivePlaybackEnabled,
        timingCsv,
        blockLedgerCsv,
        dashboardPhase: 'completed',
        translatedTransportEvidence: translatedTransportEvidenceRef.current,
      });
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
      const base = `rendered-digital-preflight-${timestamp}`;
      downloadPrivateArtifact(
        timingCsv,
        'text/csv;charset=utf-8',
        `${base}.timing.csv`,
      );
      downloadPrivateArtifact(
        blockLedgerCsv,
        'text/csv;charset=utf-8',
        `${base}.blocks.csv`,
      );
      downloadPrivateArtifact(
        `${JSON.stringify(manifest, null, 2)}\n`,
        'application/json',
        `${base}.manifest.json`,
      );
      downloadPrivateArtifact(
        capture.wavBytes,
        'audio/wav',
        `${base}.stereo.wav`,
      );
      setFailureMessage('');
    } catch (error) {
      console.error('[TestDashboard] Evidence bundle export failed:', error);
      setFailureMessage(
        error instanceof Error
          ? `Evidence export failed: ${error.message}`
          : 'Evidence export failed.',
      );
    }
  }, [adaptivePlaybackEnabled]);

  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file) {
        console.log('[TestDashboard] Loading file:', file.name, file.size, 'bytes');
        fileSourceRef.current.loadFile(file);
      }
    },
    [],
  );

  const handleNewTest = useCallback(() => {
    renderedDigitalCaptureResultRef.current = null;
    renderedDigitalSourceSnapshotRef.current = null;
    renderedDigitalSourceScheduleRef.current = null;
    captureConfigRef.current = null;
    translatedTransportEvidenceRef.current = {
      received: [],
      scheduled: [],
    };
    tailFreshnessShadowRef.current = null;
    tailFreshnessShadowEvidenceRef.current = null;
    tailFreshnessShadowErrorRef.current = null;
    setTailFreshnessShadowSummary(EMPTY_TAIL_SHADOW_SUMMARY);
    setTailFreshnessShadowStatus(
      tailFreshnessShadowEnabledRef.current ? 'ready' : 'off',
    );
    setSourceChunksSent(0);
    serverTerminalStateRef.current = 'pending';
    setServerTerminalState('pending');
    setFailureMessage('');
    setPhase('idle');
  }, []);

  const progressPct = fileSource.duration > 0
    ? (fileSource.position / fileSource.duration) * 100
    : 0;
  const renderedDigitalFatalDiagnostic = (
    renderedDigitalCaptureEnabled
    && (
      phase === 'running'
      || phase === 'draining'
      || phase === 'failed'
    )
  ) ? renderedDigitalCapture.fatalDiagnostic : null;

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-50 to-gray-100 p-6">
      <div
        className="max-w-4xl mx-auto space-y-6"
        data-s2s-phase={phase}
        data-s2s-source-chunks-sent={sourceChunksSent}
        data-s2s-server-terminal-state={serverTerminalState}
        data-s2s-tail-shadow-enabled={tailFreshnessShadowEnabled}
        data-s2s-tail-shadow-status={tailFreshnessShadowStatus}
        data-s2s-tail-shadow-hard-cap-achieved={
          tailFreshnessShadowSummary.hardCapAchieved
        }
        data-s2s-tail-shadow-single-tail-contract={
          tailFreshnessShadowSummary.singleTailContractHolds
        }
        data-s2s-recorder-fatal-code={
          renderedDigitalFatalDiagnostic?.code
        }
        data-s2s-recorder-gap-expected-context-frame={
          renderedDigitalFatalDiagnostic?.expectedContextFrame
            ?? undefined
        }
        data-s2s-recorder-gap-observed-context-frame={
          renderedDigitalFatalDiagnostic?.observedContextFrame
            ?? undefined
        }
        data-s2s-recorder-gap-delta-frames={
          renderedDigitalFatalDiagnostic?.deltaFrames ?? undefined
        }
      >
        {/* Header */}
        <div className="bg-white rounded-2xl shadow-xl p-6">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-2xl font-bold text-gray-800">Latency Test Dashboard</h1>
              <p className="text-gray-500 text-sm mt-1">
                Measure audience playback backlog and legacy duration drift
              </p>
            </div>
            <a href="#/" className="text-blue-500 hover:text-blue-700 text-sm underline">
              Back to Translation
            </a>
          </div>
        </div>

        {/* File Upload + Controls */}
        <div className="bg-white rounded-2xl shadow-xl p-6">
          <div className="flex flex-wrap items-center gap-4">
            <label className="flex-1 min-w-[200px]">
              <span className="block text-sm font-medium text-gray-700 mb-1">Audio File</span>
              <input
                type="file"
                accept=".wav,.mp3"
                data-s2s-control="audio-file"
                onChange={handleFileChange}
                disabled={phase !== 'idle'}
                className="block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-medium file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100 disabled:opacity-50"
              />
            </label>

            {phase === 'idle' && (
              <button
                onClick={handleStart}
                disabled={!fileSource.isLoaded}
                data-s2s-control="start-test"
                className="px-6 py-2 bg-blue-600 text-white rounded-lg font-medium disabled:opacity-50 disabled:cursor-not-allowed hover:bg-blue-700 transition-colors"
              >
                Start Test
              </button>
            )}

            {phase === 'starting' && (
              <p
                className="px-3 py-2 text-sm text-blue-700"
                data-s2s-status="starting"
              >
                Preparing the evidence window…
              </p>
            )}

            {(phase === 'running' || phase === 'draining') && (
              <button
                onClick={handleStop}
                data-s2s-control="stop-test"
                className="px-6 py-2 bg-red-600 text-white rounded-lg font-medium hover:bg-red-700 transition-colors"
              >
                Stop Test
              </button>
            )}

            {(phase === 'completed' || phase === 'failed') && (
              <div className="flex gap-2">
                <button
                  onClick={handleExport}
                  data-s2s-control="export-evidence"
                  className="px-6 py-2 bg-green-600 text-white rounded-lg font-medium hover:bg-green-700 transition-colors"
                >
                  {renderedDigitalCaptureEnabled
                    ? 'Export Evidence'
                    : 'Export CSV'}
                </button>
                <button
                  onClick={handleNewTest}
                  data-s2s-control="new-test"
                  className="px-6 py-2 bg-gray-200 text-gray-700 rounded-lg font-medium hover:bg-gray-300 transition-colors"
                >
                  New Test
                </button>
              </div>
            )}
          </div>

          {failureMessage && (
            <div
              role="alert"
              className="mt-4 p-3 bg-red-50 border border-red-200 rounded-lg text-red-700 text-sm"
            >
              {phase === 'failed'
                ? 'Test failed'
                : phase === 'completed'
                  ? 'Evidence issue'
                  : 'Could not start test'}:{' '}
              {failureMessage}
            </div>
          )}

          <label className="mt-4 flex items-start gap-2 text-sm text-gray-700">
            <input
              type="checkbox"
              checked={adaptivePlaybackEnabled}
              onChange={(event) => setAdaptivePlaybackEnabled(event.target.checked)}
              disabled={phase !== 'idle'}
              className="mt-0.5 h-4 w-4"
            />
            <span>
              Adaptive Spanish playback (1.00x / 1.05x / 1.10x)
              <span className="block text-xs text-gray-500">
                Turn off before starting to capture the fixed 1.00x control.
              </span>
            </span>
          </label>

          <label className="mt-4 flex items-start gap-2 text-sm text-gray-700">
            <input
              type="checkbox"
              checked={tailFreshnessShadowEnabled}
              onChange={(event) => (
                setTailFreshnessShadowEnabled(event.target.checked)
              )}
              disabled={phase !== 'idle'}
              data-s2s-control="tail-freshness-shadow"
              className="mt-0.5 h-4 w-4"
            />
            <span>
              Observation-only 10-second tail-freshness shadow
              <span className="block text-xs text-gray-500">
                Default off. Projects suffix truncations from numeric metadata;
                it never cancels, reschedules, or changes audible audio.
              </span>
            </span>
          </label>

          <label className="mt-4 flex items-start gap-2 text-sm text-gray-700">
            <input
              type="checkbox"
              checked={renderedDigitalCaptureEnabled}
              onChange={(event) => (
                setRenderedDigitalCaptureEnabled(event.target.checked)
              )}
              disabled={phase !== 'idle'}
              data-s2s-control="rendered-digital-capture"
              className="mt-0.5 h-4 w-4"
            />
            <span>
              60-second rendered-digital common-clock preflight
              <span className="block text-xs text-amber-700">
                Default off. Captures private source and translated PCM before
                the monitor mute; keep all exported audio untracked.
              </span>
            </span>
          </label>

          {renderedDigitalCapture.isCapturing && (
            <p
              className="mt-2 text-xs text-amber-700"
              data-s2s-status="rendered-digital-capturing"
            >
              Common-clock PCM capture is active.
            </p>
          )}

          {/* File Info */}
          {fileSource.isLoaded && (
            <p className="mt-2 text-xs text-gray-400">
              Duration: {(fileSource.duration / 60).toFixed(1)} min ({fileSource.duration.toFixed(0)}s)
            </p>
          )}

          {/* Audio Monitor Toggles */}
          {(phase === 'running' || phase === 'draining') && (
            <div className="mt-4 flex gap-3">
              <button
                onClick={() => inputPlayback.setMuted(!inputPlayback.isMuted)}
                className={`px-3 py-1.5 text-xs font-medium rounded-lg border transition-colors ${
                  inputPlayback.isMuted
                    ? 'border-gray-300 text-gray-500 bg-gray-50 hover:bg-gray-100'
                    : 'border-blue-300 text-blue-700 bg-blue-50 hover:bg-blue-100'
                }`}
              >
                {inputPlayback.isMuted ? '\u{1F507}' : '\u{1F50A}'} Input Audio
              </button>
              <button
                onClick={() => outputPlayback.setMuted(!outputPlayback.isMuted)}
                className={`px-3 py-1.5 text-xs font-medium rounded-lg border transition-colors ${
                  outputPlayback.isMuted
                    ? 'border-gray-300 text-gray-500 bg-gray-50 hover:bg-gray-100'
                    : 'border-green-300 text-green-700 bg-green-50 hover:bg-green-100'
                }`}
              >
                {outputPlayback.isMuted ? '\u{1F507}' : '\u{1F50A}'} Output Audio
              </button>
            </div>
          )}

          {/* Progress Bar */}
          {(phase === 'running' || phase === 'draining') && (
            <div className="mt-4">
              <div className="flex justify-between text-xs text-gray-500 mb-1">
                <span>{fileSource.position.toFixed(1)}s</span>
                <span>{fileSource.duration.toFixed(1)}s</span>
              </div>
              <div className="w-full bg-gray-200 rounded-full h-2">
                <div
                  className="bg-blue-600 h-2 rounded-full transition-all duration-300"
                  style={{ width: `${progressPct}%` }}
                />
              </div>
            </div>
          )}

          {/* Draining indicator */}
          {phase === 'draining' && (
            <div className="mt-4 p-3 bg-amber-50 border border-amber-200 rounded-lg">
              <p className="text-amber-700 text-sm">
                File input ended. Waiting for Riva to confirm completion and for
                the listener queue to drain. Network-idle countdown:{' '}
                <span className="font-mono font-bold">{drainCountdown}s</span>;
                playback queue:{' '}
                <span className="font-mono font-bold">
                  {stats.queueDepth.toFixed(1)}s
                </span>.
              </p>
            </div>
          )}
        </div>

        {/* Drift Chart */}
        {(phase === 'running' || phase === 'draining' || phase === 'completed' || phase === 'failed') && (
          <div className="bg-white rounded-2xl shadow-xl p-6">
            <h2 className="text-lg font-semibold text-gray-800 mb-4">
              Legacy Duration Drift Over Time
            </h2>
            <DriftChart data={driftData} />
          </div>
        )}

        {/* Stats Panel */}
        {(phase === 'running' || phase === 'draining' || phase === 'completed' || phase === 'failed') && (
          <div className="bg-white rounded-2xl shadow-xl p-6">
            <h2 className="text-lg font-semibold text-gray-800 mb-1">
              Audience Playback Statistics
            </h2>
            <p className="text-xs text-gray-500 mb-4">
              Queue depth is exact browser backlog. Legacy duration drift is not
              utterance-aligned end-to-end latency.
            </p>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <StatCard
                label="Current Queue"
                value={`${stats.queueDepth.toFixed(2)}s`}
                warn={stats.queueDepth > DEFAULT_PLAYBACK_POLICY.targetQueueSeconds}
                danger={stats.queueDepth > DEFAULT_PLAYBACK_POLICY.limitQueueSeconds}
              />
              <StatCard
                label="Queue p95"
                value={`${stats.queueP95.toFixed(2)}s`}
                warn={stats.queueP95 > DEFAULT_PLAYBACK_POLICY.targetQueueSeconds}
                danger={stats.queueP95 > DEFAULT_PLAYBACK_POLICY.limitQueueSeconds}
              />
              <StatCard
                label="Peak Queue"
                value={`${stats.peakQueueDepth.toFixed(2)}s`}
                warn={stats.peakQueueDepth > DEFAULT_PLAYBACK_POLICY.targetQueueSeconds}
                danger={stats.peakQueueDepth > DEFAULT_PLAYBACK_POLICY.limitQueueSeconds}
              />
              <StatCard
                label="Playback Rate"
                value={`${stats.playbackRate.toFixed(2)}x`}
              />
              <StatCard
                label="Time >5s"
                value={`${stats.secondsAboveTarget.toFixed(0)}s`}
              />
              <StatCard
                label="Time >10s"
                value={`${stats.secondsAboveLimit.toFixed(0)}s`}
                danger={stats.secondsAboveLimit > 0}
              />
              <StatCard
                label="Limit Breaches"
                value={stats.limitBreaches.toString()}
                danger={stats.limitBreaches > 0}
              />
              <StatCard label="Responses" value={stats.responsesReceived.toString()} />
            </div>

            <h3 className="text-sm font-semibold text-gray-600 mt-6 mb-3">
              Legacy Duration Comparison
            </h3>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <StatCard
                label="Current Drift"
                value={`${stats.currentDrift.toFixed(2)}s`}
                warn={stats.currentDrift > 20}
                danger={stats.currentDrift > 30}
              />
              <StatCard label="Avg Drift" value={`${stats.avgDrift.toFixed(2)}s`} />
              <StatCard
                label="Max Drift"
                value={`${stats.maxDrift.toFixed(2)}s`}
                warn={stats.maxDrift > 20}
                danger={stats.maxDrift > 30}
              />
              <StatCard label="Elapsed" value={`${(stats.elapsedSec / 60).toFixed(1)} min`} />
              <StatCard label="Chunks Sent" value={stats.chunksSent.toString()} />
            </div>
          </div>
        )}

        {tailFreshnessShadowEnabled && phase !== 'idle' && (
          <div className="bg-white rounded-2xl shadow-xl p-6">
            <h2 className="text-lg font-semibold text-gray-800 mb-1">
              Observation-only Tail-Freshness Shadow
            </h2>
            <p className="text-xs text-gray-500 mb-4">
              Projected 10-second queue policy only. Audible Spanish playback
              remains unchanged. Status: {tailFreshnessShadowStatus}.
            </p>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <StatCard
                label="Last-decision Queue"
                value={`${tailFreshnessShadowSummary.lastDecisionQueueSeconds.toFixed(2)}s`}
                danger={!tailFreshnessShadowSummary.hardCapAchieved}
              />
              <StatCard
                label="Projected Peak"
                value={`${tailFreshnessShadowSummary.peakQueueAfterTruncationSeconds.toFixed(2)}s`}
                danger={!tailFreshnessShadowSummary.hardCapAchieved}
              />
              <StatCard
                label="Projected Retained"
                value={`${tailFreshnessShadowSummary.retainedSourcePercent.toFixed(2)}%`}
              />
              <StatCard
                label="Parents Affected"
                value={(
                  tailFreshnessShadowSummary.parentsTruncated
                  + tailFreshnessShadowSummary.parentsFullyDropped
                ).toString()}
              />
              <StatCard
                label="Projected Frames Dropped"
                value={tailFreshnessShadowSummary.framesDropped.toString()}
              />
              <StatCard
                label="Longest Suffix"
                value={`${tailFreshnessShadowSummary.maxDroppedParentSuffixSeconds.toFixed(2)}s`}
              />
              <StatCard
                label="Residual Breaches"
                value={tailFreshnessShadowSummary.residualBreachEvents.toString()}
                danger={tailFreshnessShadowSummary.residualBreachEvents > 0}
              />
              <StatCard
                label="Single-tail Contract"
                value={tailFreshnessShadowSummary.singleTailContractHolds
                  ? 'Pass'
                  : 'Fail'}
                danger={!tailFreshnessShadowSummary.singleTailContractHolds}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function StatCard({
  label,
  value,
  warn = false,
  danger = false,
}: {
  label: string;
  value: string;
  warn?: boolean;
  danger?: boolean;
}) {
  const color = danger
    ? 'text-red-600'
    : warn
      ? 'text-amber-600'
      : 'text-gray-800';

  return (
    <div className="bg-gray-50 rounded-lg p-3">
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={`text-xl font-mono font-bold ${color}`}>{value}</p>
    </div>
  );
}
