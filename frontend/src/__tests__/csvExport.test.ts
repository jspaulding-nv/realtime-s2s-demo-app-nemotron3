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
  let capturedCsvText: string;
  let mockAnchor: {
    href: string;
    download: string;
    style: { display: string };
    click: ReturnType<typeof vi.fn>;
  };

  const OriginalBlob = globalThis.Blob;

  beforeEach(() => {
    capturedCsvText = '';

    mockAnchor = {
      href: '',
      download: '',
      style: { display: '' },
      click: vi.fn(),
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
    expect(fields.slice(6, 13)).toEqual([
      '0.100000',
      '0.095238',
      '5.200000',
      '5.295238',
      '1.05',
      'catch-up',
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
      inputLedgerValid: true,
      sourceEndBoundaryClientMs: 1500,
      sourceEndToBinaryReceiptMs: 300,
      schedulePerformanceClientMs: 1801,
      audioContextTimeAtScheduleSec: 10,
      scheduledStartContextSec: 12,
      scheduledEndContextSec: 12.1,
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
      input_ledger_valid: 'true',
      source_end_boundary_client_ms: '1500.000',
      source_end_to_binary_receipt_ms: '300.000',
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
