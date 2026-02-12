import { useCallback, useEffect, useRef, useState } from 'react';
import { useWebSocket } from '../hooks/useWebSocket';
import { useFileAudioSource } from '../hooks/useFileAudioSource';
import { useTimingTracker } from '../hooks/useTimingTracker';
import { useMetricsSocket } from '../hooks/useMetricsSocket';
import { DriftChart } from './DriftChart';
import { exportTimingDataAsCSV } from '../utils/csvExport';
import type { DriftDataPoint } from '../types/timing';

type TestPhase = 'idle' | 'running' | 'completed';

export function TestDashboard() {
  const [phase, setPhase] = useState<TestPhase>('idle');
  const [driftData, setDriftData] = useState<DriftDataPoint[]>([]);
  const [stats, setStats] = useState({
    currentDrift: 0,
    avgDrift: 0,
    maxDrift: 0,
    elapsedSec: 0,
    chunksSent: 0,
    responsesReceived: 0,
  });

  const initialDriftRef = useRef<number | null>(null);
  const testStartTimeRef = useRef(0);
  const driftUpdateTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const tracker = useTimingTracker();
  const metrics = useMetricsSocket();

  // WebSocket for audio transport (reuse existing infrastructure)
  const ws = useWebSocket({
    url: `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws/translate`,
    onAudio: (audio: ArrayBuffer) => {
      // Silent mode: only track timing, don't play audio
      tracker.logAudioReceived(audio.byteLength);
    },
  });

  // File audio source
  const fileSource = useFileAudioSource({
    onChunk: (chunk: ArrayBuffer) => {
      ws.sendAudio(chunk);
      tracker.logChunkSent(chunk.byteLength);
    },
    onComplete: () => {
      handleStop();
    },
  });

  const updateDriftData = useCallback(() => {
    const elapsedMs = performance.now() - testStartTimeRef.current;
    const elapsedSec = elapsedMs / 1000;
    const cumulativeOutput = tracker.getCumulativeOutputDuration();
    const rawDrift = elapsedSec - cumulativeOutput;

    if (initialDriftRef.current === null && cumulativeOutput > 0) {
      initialDriftRef.current = rawDrift;
    }

    const accumulatingDrift =
      initialDriftRef.current !== null ? rawDrift - initialDriftRef.current : 0;

    const point: DriftDataPoint = {
      elapsedMinutes: elapsedSec / 60,
      rawDriftSeconds: rawDrift,
      driftSeconds: accumulatingDrift,
    };

    setDriftData((prev) => [...prev, point]);
    setStats({
      currentDrift: accumulatingDrift,
      avgDrift:
        driftData.length > 0
          ? driftData.reduce((s, d) => s + d.driftSeconds, 0) / driftData.length
          : 0,
      maxDrift:
        driftData.length > 0
          ? Math.max(...driftData.map((d) => Math.abs(d.driftSeconds)))
          : 0,
      elapsedSec,
      chunksSent: tracker.getSendCount(),
      responsesReceived: tracker.getReceiveCount(),
    });
  }, [tracker, driftData]);

  const handleStart = useCallback(async () => {
    // Reset state
    setDriftData([]);
    initialDriftRef.current = null;
    testStartTimeRef.current = performance.now();

    // Start backend test mode
    await fetch('/api/test/start', { method: 'POST' });

    // Connect metrics socket
    metrics.connect();
    metrics.clearEvents();

    // Reset timing tracker
    tracker.startTest();

    // Connect translation WebSocket and start stream
    ws.connect();

    // Wait for WebSocket to connect before starting stream
    setPhase('running');
  }, [metrics, tracker, ws]);

  // Once WebSocket connects while running, start the stream and file source
  const hasStartedStreamRef = useRef(false);
  useEffect(() => {
    if (phase === 'running' && ws.isConnected && !hasStartedStreamRef.current) {
      hasStartedStreamRef.current = true;
      ws.sendMessage({ type: 'start_stream', targetLanguage: 'es-US' });
      // Small delay to let stream initialize
      setTimeout(() => {
        fileSource.startStreaming();
      }, 500);
    }
    if (phase !== 'running') {
      hasStartedStreamRef.current = false;
    }
  }, [phase, ws.isConnected, ws, fileSource]);

  // Update drift data periodically
  useEffect(() => {
    if (phase === 'running') {
      driftUpdateTimerRef.current = setInterval(updateDriftData, 1000);
      return () => {
        if (driftUpdateTimerRef.current) clearInterval(driftUpdateTimerRef.current);
      };
    }
  }, [phase, updateDriftData]);

  const handleStop = useCallback(async () => {
    fileSource.stopStreaming();
    ws.sendMessage({ type: 'stop_stream' });
    ws.disconnect();
    metrics.disconnect();
    await fetch('/api/test/stop', { method: 'POST' });

    if (driftUpdateTimerRef.current) {
      clearInterval(driftUpdateTimerRef.current);
      driftUpdateTimerRef.current = null;
    }

    setPhase('completed');
  }, [fileSource, ws, metrics]);

  const handleExport = useCallback(async () => {
    // Fetch backend events
    const resp = await fetch('/api/test/export');
    const data = await resp.json();
    exportTimingDataAsCSV(tracker.getEvents(), data.events);
  }, [tracker]);

  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file) fileSource.loadFile(file);
    },
    [fileSource],
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
                Measure drift between input and output audio over time
              </p>
            </div>
            <a
              href="#/"
              className="text-blue-500 hover:text-blue-700 text-sm underline"
            >
              Back to Translation
            </a>
          </div>
        </div>

        {/* File Upload + Controls */}
        <div className="bg-white rounded-2xl shadow-xl p-6">
          <div className="flex flex-wrap items-center gap-4">
            <label className="flex-1 min-w-[200px]">
              <span className="block text-sm font-medium text-gray-700 mb-1">
                Audio File
              </span>
              <input
                type="file"
                accept=".wav,.mp3"
                onChange={handleFileChange}
                disabled={phase === 'running'}
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

            {phase === 'running' && (
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

          {/* File Info */}
          {fileSource.isLoaded && (
            <p className="mt-2 text-xs text-gray-400">
              Duration: {(fileSource.duration / 60).toFixed(1)} min ({fileSource.duration.toFixed(0)}s)
            </p>
          )}

          {/* Progress Bar */}
          {phase === 'running' && (
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
        </div>

        {/* Drift Chart */}
        {(phase === 'running' || phase === 'completed') && (
          <div className="bg-white rounded-2xl shadow-xl p-6">
            <h2 className="text-lg font-semibold text-gray-800 mb-4">Drift Over Time</h2>
            <DriftChart data={driftData} />
          </div>
        )}

        {/* Stats Panel */}
        {(phase === 'running' || phase === 'completed') && (
          <div className="bg-white rounded-2xl shadow-xl p-6">
            <h2 className="text-lg font-semibold text-gray-800 mb-4">Statistics</h2>
            <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
              <StatCard
                label="Current Drift"
                value={`${stats.currentDrift.toFixed(2)}s`}
                warn={Math.abs(stats.currentDrift) > 20}
                danger={Math.abs(stats.currentDrift) > 30}
              />
              <StatCard
                label="Avg Drift"
                value={`${stats.avgDrift.toFixed(2)}s`}
              />
              <StatCard
                label="Max Drift"
                value={`${stats.maxDrift.toFixed(2)}s`}
                warn={stats.maxDrift > 20}
                danger={stats.maxDrift > 30}
              />
              <StatCard
                label="Elapsed"
                value={`${(stats.elapsedSec / 60).toFixed(1)} min`}
              />
              <StatCard
                label="Chunks Sent"
                value={stats.chunksSent.toString()}
              />
              <StatCard
                label="Responses"
                value={stats.responsesReceived.toString()}
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
