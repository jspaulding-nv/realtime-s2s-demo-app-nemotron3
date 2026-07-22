import { useCallback, useEffect, useRef, useState } from 'react';
import { useWebSocket } from '../hooks/useWebSocket';
import { useFileAudioSource } from '../hooks/useFileAudioSource';
import { useTimingTracker } from '../hooks/useTimingTracker';
import { useMetricsSocket } from '../hooks/useMetricsSocket';
import { useAudioPlayback } from '../hooks/useAudioPlayback';
import { DriftChart } from './DriftChart';
import { exportTimingDataAsCSV } from '../utils/csvExport';
import type { DriftDataPoint } from '../types/timing';
import {
  DEFAULT_PLAYBACK_POLICY,
  summarizePlaybackQueue,
  type PlaybackQueueSample,
} from '../utils/playbackPolicy';

type TestPhase = 'idle' | 'running' | 'draining' | 'completed';

const DRAIN_MIN_SEC = 10;
const DRAIN_IDLE_SEC = 5;
const DRAIN_MAX_SEC = 300;
const PLAYBACK_DRAIN_EPSILON_SEC = 0.1;

export function TestDashboard() {
  const [phase, setPhase] = useState<TestPhase>('idle');
  const [adaptivePlaybackEnabled, setAdaptivePlaybackEnabled] = useState(true);
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
  const lastReceiveChangeRef = useRef(0);
  const drainStartTimeRef = useRef(0);
  const phaseRef = useRef<TestPhase>('idle');
  const driftDataRef = useRef<DriftDataPoint[]>([]);
  const queueSamplesRef = useRef<PlaybackQueueSample[]>([]);

  const tracker = useTimingTracker();
  const trackerRef = useRef(tracker);
  const metrics = useMetricsSocket();

  // Audio playback: input (English) starts muted, output (Spanish) starts muted
  const inputPlayback = useAudioPlayback({
    sampleRate: 16000,
    initialMuted: true,
    adaptivePlayback: false,
  });
  const outputPlayback = useAudioPlayback({
    sampleRate: 16000,
    initialMuted: true,
    adaptivePlayback: adaptivePlaybackEnabled,
    onSchedule: (event) => trackerRef.current.logPlaybackScheduled(event),
  });

  // Stable refs for playback instances
  const inputPlaybackRef = useRef(inputPlayback);
  const outputPlaybackRef = useRef(outputPlayback);

  useEffect(() => {
    trackerRef.current = tracker;
    inputPlaybackRef.current = inputPlayback;
    outputPlaybackRef.current = outputPlayback;
  }, [tracker, inputPlayback, outputPlayback]);

  // Keep refs in sync with state
  useEffect(() => {
    phaseRef.current = phase;
    console.log('[TestDashboard] Phase changed to:', phase);
  }, [phase]);
  useEffect(() => { driftDataRef.current = driftData; }, [driftData]);

  // Store stable function refs to avoid closure issues
  const wsRef = useRef<ReturnType<typeof useWebSocket>>(null!);
  const fileSourceRef = useRef<ReturnType<typeof useFileAudioSource>>(null!);

  // Chunk counter for logging (doesn't need to trigger re-renders)
  const chunkLogCountRef = useRef(0);
  const audioLogCountRef = useRef(0);

  // WebSocket for audio transport
  const ws = useWebSocket({
    url: `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws/translate`,
    onAudio: (audio: ArrayBuffer) => {
      audioLogCountRef.current += 1;
      if (audioLogCountRef.current <= 5 || audioLogCountRef.current % 50 === 0) {
        console.log(`[TestDashboard] onAudio #${audioLogCountRef.current}: ${audio.byteLength} bytes`);
      }
      trackerRef.current.logAudioReceived(audio.byteLength);
      lastReceiveChangeRef.current = performance.now();
      // Queue translated audio for output playback
      outputPlaybackRef.current.queueAudio(audio);
    },
  });

  useEffect(() => {
    wsRef.current = ws;
  }, [ws]);

  // File audio source
  const fileSource = useFileAudioSource({
    onChunk: (chunk: ArrayBuffer) => {
      chunkLogCountRef.current += 1;
      if (chunkLogCountRef.current <= 5 || chunkLogCountRef.current % 50 === 0) {
        console.log(`[TestDashboard] onChunk #${chunkLogCountRef.current}: ${chunk.byteLength} bytes`);
      }
      wsRef.current.sendAudio(chunk);
      trackerRef.current.logChunkSent(chunk.byteLength);
      // Queue input audio for input playback monitoring
      inputPlaybackRef.current.queueAudio(chunk);
    },
    onComplete: () => {
      console.log('[TestDashboard] onComplete fired. phaseRef.current =', phaseRef.current);
      if (phaseRef.current === 'running') {
        // Close the request-audio side while leaving the WebSocket open for
        // final Riva responses and the browser playback queue to drain.
        wsRef.current.sendMessage({ type: 'end_input' });
        lastReceiveChangeRef.current = performance.now();
        drainStartTimeRef.current = performance.now();
        setPhase('draining');
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
    setDriftData([]);
    driftDataRef.current = [];
    queueSamplesRef.current = [];
    testStartTimeRef.current = performance.now();
    lastReceiveChangeRef.current = performance.now();
    chunkLogCountRef.current = 0;
    audioLogCountRef.current = 0;
    setDrainCountdown(DRAIN_IDLE_SEC);

    console.log('[TestDashboard] Calling /api/test/start...');
    await fetch('/api/test/start', { method: 'POST' });
    console.log('[TestDashboard] /api/test/start returned');

    metrics.connect();
    metrics.clearEvents();
    trackerRef.current.startTest({ adaptivePlaybackEnabled });

    // Start both playback instances
    inputPlaybackRef.current.start();
    outputPlaybackRef.current.start();

    console.log('[TestDashboard] Calling ws.connect()...');
    wsRef.current.connect();
    setPhase('running');
  }, [metrics, adaptivePlaybackEnabled]);

  // -- Once WS connects, start stream + file source --
  const hasStartedStreamRef = useRef(false);
  useEffect(() => {
    if (phase === 'running' && ws.isConnected && !hasStartedStreamRef.current) {
      hasStartedStreamRef.current = true;
      console.log('[TestDashboard] WS connected during running phase, sending start_stream');
      wsRef.current.sendMessage({ type: 'start_stream', targetLanguage: 'es-US' });
      console.log('[TestDashboard] Will start file streaming in 500ms');
      setTimeout(() => {
        console.log('[TestDashboard] Starting file streaming now');
        fileSourceRef.current.startStreaming();
      }, 500);
    }
    if (phase !== 'running' && phase !== 'draining') {
      hasStartedStreamRef.current = false;
    }
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

  // -- Finish: disconnect everything, move to completed --
  const finishTestRef = useRef<(() => Promise<void>) | null>(null);
  const finishTest = useCallback(async () => {
    console.log('[TestDashboard] finishTest called');
    wsRef.current.sendMessage({ type: 'stop_stream' });
    wsRef.current.disconnect();
    metrics.disconnect();

    // Stop both playback instances
    inputPlaybackRef.current.stop();
    outputPlaybackRef.current.stop();

    await fetch('/api/test/stop', { method: 'POST' });
    if (driftUpdateTimerRef.current) {
      clearInterval(driftUpdateTimerRef.current);
      driftUpdateTimerRef.current = null;
    }
    setPhase('completed');
  }, [metrics]);

  useEffect(() => {
    finishTestRef.current = finishTest;
  }, [finishTest]);

  // -- Draining: require both network quiet and an empty playback queue --
  useEffect(() => {
    if (phase !== 'draining') return;

    console.log(
      '[TestDashboard] Draining phase started; waiting for network and playback queue',
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
        drainElapsedSec >= DRAIN_MIN_SEC
        && silenceSec >= DRAIN_IDLE_SEC
        && queueDepthSec <= PLAYBACK_DRAIN_EPSILON_SEC;
      const timedOut = drainElapsedSec >= DRAIN_MAX_SEC;

      if (fullyDrained || timedOut) {
        console.log(
          fullyDrained
            ? '[TestDashboard] Network and playback queue drained'
            : '[TestDashboard] Maximum drain time reached',
        );
        clearInterval(checkInterval);
        finishTestRef.current?.();
      }
    }, 1000);

    return () => clearInterval(checkInterval);
  }, [phase]);

  // -- Manual stop --
  const handleStop = useCallback(async () => {
    console.log('[TestDashboard] handleStop called');
    fileSourceRef.current.stopStreaming();
    await finishTestRef.current?.();
  }, []);

  // -- CSV export --
  const handleExport = useCallback(async () => {
    console.log('[TestDashboard] handleExport called');
    try {
      const resp = await fetch('/api/test/export');
      const data = await resp.json();
      const clientEvents = trackerRef.current.getEvents();
      console.log(`[TestDashboard] Export: ${clientEvents.length} client events, ${data.events?.length ?? 0} backend events`);
      exportTimingDataAsCSV(clientEvents, data.events || []);
    } catch (err) {
      console.error('[TestDashboard] Export failed:', err);
      const clientEvents = trackerRef.current.getEvents();
      console.log(`[TestDashboard] Fallback export with ${clientEvents.length} client events`);
      exportTimingDataAsCSV(clientEvents, []);
    }
  }, []);

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

  const progressPct = fileSource.duration > 0
    ? (fileSource.position / fileSource.duration) * 100
    : 0;

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-50 to-gray-100 p-6">
      <div className="max-w-4xl mx-auto space-y-6">
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
                onChange={handleFileChange}
                disabled={phase === 'running' || phase === 'draining'}
                className="block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-medium file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100 disabled:opacity-50"
              />
            </label>

            {phase === 'idle' && (
              <button
                onClick={handleStart}
                disabled={!fileSource.isLoaded}
                className="px-6 py-2 bg-blue-600 text-white rounded-lg font-medium disabled:opacity-50 disabled:cursor-not-allowed hover:bg-blue-700 transition-colors"
              >
                Start Test
              </button>
            )}

            {(phase === 'running' || phase === 'draining') && (
              <button
                onClick={handleStop}
                className="px-6 py-2 bg-red-600 text-white rounded-lg font-medium hover:bg-red-700 transition-colors"
              >
                Stop Test
              </button>
            )}

            {phase === 'completed' && (
              <div className="flex gap-2">
                <button
                  onClick={handleExport}
                  className="px-6 py-2 bg-green-600 text-white rounded-lg font-medium hover:bg-green-700 transition-colors"
                >
                  Export CSV
                </button>
                <button
                  onClick={() => setPhase('idle')}
                  className="px-6 py-2 bg-gray-200 text-gray-700 rounded-lg font-medium hover:bg-gray-300 transition-colors"
                >
                  New Test
                </button>
              </div>
            )}
          </div>

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
                File input ended. Waiting for Riva output and the listener queue
                to drain. Network-idle countdown:{' '}
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
        {(phase === 'running' || phase === 'draining' || phase === 'completed') && (
          <div className="bg-white rounded-2xl shadow-xl p-6">
            <h2 className="text-lg font-semibold text-gray-800 mb-4">
              Legacy Duration Drift Over Time
            </h2>
            <DriftChart data={driftData} />
          </div>
        )}

        {/* Stats Panel */}
        {(phase === 'running' || phase === 'draining' || phase === 'completed') && (
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
