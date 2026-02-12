import type { ClientTimingEvent, BackendTimingEvent } from '../types/timing';

export function exportTimingDataAsCSV(
  clientEvents: ClientTimingEvent[],
  backendEvents: BackendTimingEvent[],
): void {
  const header = 'source,stage,timestamp_ms,chunk_index,source_position_sec,audio_bytes';
  const rows: string[] = [header];

  for (const e of clientEvents) {
    rows.push(
      `client,${e.stage},${e.timestamp.toFixed(2)},${e.chunkIndex},${e.sourcePositionSec.toFixed(3)},${e.audioBytes}`,
    );
  }

  for (const e of backendEvents) {
    rows.push(
      `backend,${e.stage},${(e.wall_clock * 1000).toFixed(2)},${e.chunk_index},${e.source_position_sec.toFixed(3)},${e.audio_bytes_len}`,
    );
  }

  const csv = rows.join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  a.href = url;
  a.download = `timing-export-${timestamp}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}
