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
    'audio_metadata_protocol_version',
    'stream_generation',
    'parent_sequence_id',
    'audio_frame_id',
    'audio_frame_count',
    'source_start_ms',
    'source_end_ms',
    'source_timing_basis',
    'binary_receipt_client_ms',
    'parent_complete_received_client_ms',
    'input_sample_zero_client_ms',
    'input_chunk_emitted_client_ms',
    'input_source_sample_start',
    'input_source_sample_end_exclusive',
    'input_sample_rate_hz',
    'input_ledger_valid',
    'source_end_boundary_client_ms',
    'source_end_to_binary_receipt_ms',
    'source_end_to_parent_complete_ms',
    'schedule_performance_client_ms',
    'audio_context_time_at_schedule_sec',
    'scheduled_start_context_sec',
    'scheduled_end_context_sec',
    'projected_scheduled_start_client_ms',
    'source_end_to_projected_scheduled_start_ms',
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
        e.audioMetadataProtocolVersion ?? '',
        e.streamGeneration ?? '',
        e.parentSequenceId ?? '',
        e.audioFrameId ?? '',
        e.audioFrameCount ?? '',
        e.sourceStartMs === null
          ? 'null'
          : (e.sourceStartMs?.toFixed(3) ?? ''),
        e.sourceEndMs === null
          ? 'null'
          : (e.sourceEndMs?.toFixed(3) ?? ''),
        e.sourceTimingBasis ?? '',
        e.binaryReceiptClientMs?.toFixed(3) ?? '',
        e.parentCompleteReceivedClientMs?.toFixed(3) ?? '',
        e.inputSampleZeroClientMs?.toFixed(3) ?? '',
        e.inputChunkEmittedClientMs?.toFixed(3) ?? '',
        e.inputSourceSampleStart ?? '',
        e.inputSourceSampleEndExclusive ?? '',
        e.inputSampleRateHz ?? '',
        e.inputLedgerValid === undefined ? '' : String(e.inputLedgerValid),
        e.sourceEndBoundaryClientMs?.toFixed(3) ?? '',
        e.sourceEndToBinaryReceiptMs?.toFixed(3) ?? '',
        e.sourceEndToParentCompleteMs?.toFixed(3) ?? '',
        e.schedulePerformanceClientMs?.toFixed(3) ?? '',
        e.audioContextTimeAtScheduleSec?.toFixed(6) ?? '',
        e.scheduledStartContextSec?.toFixed(6) ?? '',
        e.scheduledEndContextSec?.toFixed(6) ?? '',
        e.projectedScheduledStartClientMs?.toFixed(3) ?? '',
        e.sourceEndToProjectedScheduledStartMs?.toFixed(3) ?? '',
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
        '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', '',
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
