import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { exportTimingDataAsCSV } from '../utils/csvExport';
import type { ClientTimingEvent, BackendTimingEvent } from '../types/timing';

describe('exportTimingDataAsCSV', () => {
  const expectedHeader = [
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
    'terminal_status',
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
    'input_pcm_sha256',
    'input_pcm_sample_count',
    'input_ledger_valid',
    'input_source_boundary_context_frame',
    'input_source_boundary_delivered_after_context_frame',
    'input_source_boundary_received_context_frame_before',
    'input_source_boundary_received_context_frame_after',
    'input_source_boundary_received_client_ms',
    'input_chunk_emitted_context_frame',
    'source_end_boundary_client_ms',
    'source_end_to_binary_receipt_ms',
    'source_end_to_parent_complete_ms',
    'schedule_performance_client_ms',
    'audio_context_time_at_schedule_sec',
    'scheduled_start_context_sec',
    'scheduled_end_context_sec',
    'scheduled_start_context_frame_floor',
    'scheduled_end_context_frame_exclusive',
    'projected_scheduled_start_client_ms',
    'source_end_to_projected_scheduled_start_ms',
    'playback_clock_session_id',
    'clock_sample_sequence',
    'clock_sample_reason',
    'clock_sample_performance_client_ms',
    'clock_sample_performance_before_client_ms',
    'clock_sample_performance_after_client_ms',
    'clock_sample_context_sec',
    'clock_sample_output_context_sec',
    'clock_sample_output_performance_client_ms',
    'clock_sample_basis',
    'clock_sample_queue_end_context_sec',
  ].join(',');
  let capturedCsvText: string;
  let mockAnchor: {
    href: string;
    download: string;
    style: { display: string };
    click: ReturnType<typeof vi.fn>;
    remove: ReturnType<typeof vi.fn>;
  };

  const OriginalBlob = globalThis.Blob;

  beforeEach(() => {
    capturedCsvText = '';

    mockAnchor = {
      href: '',
      download: '',
      style: { display: '' },
      click: vi.fn(),
      remove: vi.fn(),
    };

    vi.spyOn(document, 'createElement').mockReturnValue(mockAnchor as unknown as HTMLElement);
    vi.spyOn(document.body, 'appendChild').mockImplementation((node) => node);
    vi.spyOn(document.body, 'removeChild').mockImplementation((node) => node);

    // Intercept Blob constructor to capture CSV text
    vi.stubGlobal('Blob', class extends OriginalBlob {
      constructor(parts?: BlobPart[], options?: BlobPropertyBag) {
        super(parts, options);
        if (parts && parts.length > 0 && typeof parts[0] === 'string') {
          capturedCsvText = parts[0];
        }
      }
    });

    URL.createObjectURL = vi.fn(() => 'blob:mock-url');
    URL.revokeObjectURL = vi.fn();
  });

  it('exports browser clock evidence with conservative brackets', () => {
    const clientEvents: ClientTimingEvent[] = [{
      stage: 'playback_clock_sample',
      timestamp: 234.25,
      chunkIndex: -1,
      sourcePositionSec: 0,
      audioBytes: 0,
      playbackClockSessionId: 7,
      clockSampleSequence: 3,
      clockSampleReason: 'interval',
      clockSamplePerformanceClientMs: 1234.25,
      clockSamplePerformanceBeforeClientMs: 1234.2,
      clockSamplePerformanceAfterClientMs: 1234.3,
      clockSampleContextSec: 2.5,
      clockSampleOutputContextSec: 2.48,
      clockSampleOutputPerformanceClientMs: 1214.1,
      clockSampleBasis: 'get_output_timestamp',
      clockSampleQueueEndContextSec: 5.75,
    }];

    exportTimingDataAsCSV(clientEvents, []);

    const headers = capturedCsvText.split('\n')[0].split(',');
    const fields = capturedCsvText.split('\n')[1].split(',');
    const row = Object.fromEntries(
      headers.map((header, index) => [header, fields[index]]),
    );
    expect(row).toMatchObject({
      playback_clock_session_id: '7',
      clock_sample_sequence: '3',
      clock_sample_reason: 'interval',
      clock_sample_performance_client_ms: '1234.250',
      clock_sample_performance_before_client_ms: '1234.200',
      clock_sample_performance_after_client_ms: '1234.300',
      clock_sample_context_sec: '2.500000',
      clock_sample_output_context_sec: '2.480000',
      clock_sample_output_performance_client_ms: '1214.100',
      clock_sample_basis: 'get_output_timestamp',
      clock_sample_queue_end_context_sec: '5.750000',
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal('Blob', OriginalBlob);
    URL.createObjectURL = () => 'blob:mock-url';
    URL.revokeObjectURL = () => {};
  });

  it('generates CSV with correct header and triggers download', () => {
    exportTimingDataAsCSV([], []);

    expect(document.createElement).toHaveBeenCalledWith('a');
    expect(mockAnchor.click).toHaveBeenCalled();
    expect(mockAnchor.download).toMatch(/^timing-export-/);
    expect(mockAnchor.download).toMatch(/\.csv$/);
    expect(mockAnchor.href).toBe('blob:mock-url');
    expect(mockAnchor.style.display).toBe('none');
    expect(document.body.appendChild).toHaveBeenCalled();
  });

  it('includes client events in CSV', () => {
    const clientEvents: ClientTimingEvent[] = [
      { stage: 'chunk_sent', timestamp: 100.5, chunkIndex: 0, sourcePositionSec: 0.3, audioBytes: 9600 },
      { stage: 'audio_received', timestamp: 200.5, chunkIndex: 0, sourcePositionSec: 0, audioBytes: 32000 },
    ];

    exportTimingDataAsCSV(clientEvents, []);

    const lines = capturedCsvText.split('\n');
    expect(lines[0]).toBe(expectedHeader);
    expect(lines[1]).toContain('client,chunk_sent');
    expect(lines[2]).toContain('client,audio_received');
    expect(lines).toHaveLength(3);
  });

  it('includes backend events in CSV', () => {
    const backendEvents: BackendTimingEvent[] = [
      { stage: 'audio_received', timestamp: 0, chunk_index: 0, source_position_sec: 0.3, audio_bytes_len: 9600, wall_clock: 1.5 },
    ];

    exportTimingDataAsCSV([], backendEvents);

    const lines = capturedCsvText.split('\n');
    expect(lines[0]).toBe(expectedHeader);
    expect(lines[1]).toContain('backend,audio_received');
    expect(lines[1].split(',')).toHaveLength(
      expectedHeader.split(',').length,
    );
    expect(lines).toHaveLength(2);
  });

  it('produces header-only CSV for empty data', () => {
    exportTimingDataAsCSV([], []);

    const lines = capturedCsvText.split('\n');
    expect(lines).toHaveLength(1);
    expect(lines[0]).toBe(expectedHeader);
  });

  it('includes both client and backend events together', () => {
    const clientEvents: ClientTimingEvent[] = [
      { stage: 'chunk_sent', timestamp: 100, chunkIndex: 0, sourcePositionSec: 0.3, audioBytes: 9600 },
    ];
    const backendEvents: BackendTimingEvent[] = [
      { stage: 'audio_received', timestamp: 0, chunk_index: 0, source_position_sec: 0.3, audio_bytes_len: 9600, wall_clock: 1.5 },
    ];

    exportTimingDataAsCSV(clientEvents, backendEvents);

    const lines = capturedCsvText.split('\n');
    expect(lines).toHaveLength(3); // header + 1 client + 1 backend
    expect(lines[1]).toContain('client,');
    expect(lines[2]).toContain('backend,');
  });

  it('exports adaptive playback telemetry columns', () => {
    const clientEvents: ClientTimingEvent[] = [{
      stage: 'playback_chunk_scheduled',
      timestamp: 250,
      chunkIndex: 3,
      sourcePositionSec: 0,
      audioBytes: 3200,
      mediaDurationSec: 0.1,
      scheduledDurationSec: 0.095238,
      playbackWaitSec: 5.2,
      queueDepthSec: 5.295238,
      playbackRate: 1.05,
      playbackMode: 'catch-up',
      adaptivePlaybackEnabled: true,
    }];

    exportTimingDataAsCSV(clientEvents, []);

    const fields = capturedCsvText.split('\n')[1].split(',');
    expect(fields.slice(6, 14)).toEqual([
      '0.100000',
      '0.095238',
      '5.200000',
      '5.295238',
      '1.05',
      'catch-up',
      '',
      'true',
    ]);
  });

  it('exports numeric metadata, ledger, and same-clock delay fields', () => {
    const clientEvents: ClientTimingEvent[] = [{
      stage: 'playback_chunk_scheduled',
      timestamp: 801,
      chunkIndex: 3,
      sourcePositionSec: 0,
      audioBytes: 3200,
      audioMetadataProtocolVersion: 1,
      streamGeneration: 4,
      parentSequenceId: 2,
      audioFrameId: 3,
      sourceStartMs: null,
      sourceEndMs: 500,
      sourceTimingBasis: 'audio_processed/nonsemantic',
      binaryReceiptClientMs: 1800,
      inputSampleZeroClientMs: 1000,
      inputPcmSha256: 'a'.repeat(64),
      inputPcmSampleCount: 19200,
      inputLedgerValid: true,
      inputSourceBoundaryReceivedContextFrameBefore: 8000,
      inputSourceBoundaryReceivedContextFrameAfter: 8000,
      inputChunkEmittedContextFrame: 8010,
      sourceEndBoundaryClientMs: 1500,
      sourceEndToBinaryReceiptMs: 300,
      schedulePerformanceClientMs: 1801,
      audioContextTimeAtScheduleSec: 10,
      scheduledStartContextSec: 12,
      scheduledEndContextSec: 12.1,
      scheduledStartContextFrameFloor: 192000,
      scheduledEndContextFrameExclusive: 193600,
      projectedScheduledStartClientMs: 3801,
      sourceEndToProjectedScheduledStartMs: 2301,
    }];

    exportTimingDataAsCSV(clientEvents, []);

    const headers = capturedCsvText.split('\n')[0].split(',');
    const fields = capturedCsvText.split('\n')[1].split(',');
    const row = Object.fromEntries(
      headers.map((header, index) => [header, fields[index]]),
    );
    expect(row).toMatchObject({
      audio_metadata_protocol_version: '1',
      stream_generation: '4',
      parent_sequence_id: '2',
      audio_frame_id: '3',
      source_start_ms: 'null',
      source_end_ms: '500.000',
      source_timing_basis: 'audio_processed/nonsemantic',
      binary_receipt_client_ms: '1800.000',
      input_sample_zero_client_ms: '1000.000',
      input_pcm_sha256: 'a'.repeat(64),
      input_pcm_sample_count: '19200',
      input_ledger_valid: 'true',
      input_source_boundary_received_context_frame_before: '8000',
      input_source_boundary_received_context_frame_after: '8000',
      input_chunk_emitted_context_frame: '8010',
      source_end_boundary_client_ms: '1500.000',
      source_end_to_binary_receipt_ms: '300.000',
      scheduled_start_context_frame_floor: '192000',
      scheduled_end_context_frame_exclusive: '193600',
      projected_scheduled_start_client_ms: '3801.000',
      source_end_to_projected_scheduled_start_ms: '2301.000',
    });
  });

  it('keeps unavailable same-clock measurements blank', () => {
    const clientEvents: ClientTimingEvent[] = [{
      stage: 'audio_received',
      timestamp: 10,
      chunkIndex: 0,
      sourcePositionSec: 0,
      audioBytes: 3200,
      audioMetadataProtocolVersion: 1,
      streamGeneration: 1,
      parentSequenceId: 0,
      audioFrameId: 0,
      sourceStartMs: null,
      sourceEndMs: null,
      sourceTimingBasis: 'unavailable',
      binaryReceiptClientMs: 100,
    }];

    exportTimingDataAsCSV(clientEvents, []);
    const headers = capturedCsvText.split('\n')[0].split(',');
    const fields = capturedCsvText.split('\n')[1].split(',');
    const row = Object.fromEntries(
      headers.map((header, index) => [header, fields[index]]),
    );
    expect(row.source_end_ms).toBe('null');
    expect(row.source_end_boundary_client_ms).toBe('');
    expect(row.source_end_to_binary_receipt_ms).toBe('');
    expect(row.source_end_to_projected_scheduled_start_ms).toBe('');
  });
});
