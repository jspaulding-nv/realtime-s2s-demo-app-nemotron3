import type { ClientTimingEvent, BackendTimingEvent } from '../types/timing';

export function exportTimingDataAsCSV(
  clientEvents: ClientTimingEvent[],
  backendEvents: BackendTimingEvent[],
): void {
  const header = [
    'source',
    'stage',
    'timestamp_ms',
    'chunk_index',
    'source_position_sec',
    'audio_bytes',
    'media_duration_sec',
    'scheduled_duration_sec',
    'playback_wait_sec',
    'queue_depth_sec',
    'playback_rate',
    'playback_mode',
    'adaptive_playback_enabled',
  ].join(',');
  const rows: string[] = [header];

  for (const e of clientEvents) {
    rows.push(
      [
        'client',
        e.stage,
        e.timestamp.toFixed(2),
        e.chunkIndex,
        e.sourcePositionSec.toFixed(3),
        e.audioBytes,
        e.mediaDurationSec?.toFixed(6) ?? '',
        e.scheduledDurationSec?.toFixed(6) ?? '',
        e.playbackWaitSec?.toFixed(6) ?? '',
        e.queueDepthSec?.toFixed(6) ?? '',
        e.playbackRate?.toFixed(2) ?? '',
        e.playbackMode ?? '',
        e.adaptivePlaybackEnabled === undefined
          ? ''
          : String(e.adaptivePlaybackEnabled),
      ].join(','),
    );
  }

  for (const e of backendEvents) {
    rows.push(
      [
        'backend',
        e.stage,
        (e.wall_clock * 1000).toFixed(2),
        e.chunk_index,
        e.source_position_sec.toFixed(3),
        e.audio_bytes_len,
        '', '', '', '', '', '', '',
      ].join(','),
    );
  }

  const csv = rows.join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');

  const a = document.createElement('a');
  a.href = url;
  a.download = `timing-export-${timestamp}.csv`;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  // Clean up after a short delay to ensure download starts
  setTimeout(() => {
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, 100);
}
