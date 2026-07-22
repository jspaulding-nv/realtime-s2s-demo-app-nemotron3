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
    expect(fields.slice(6)).toEqual([
      '0.100000',
      '0.095238',
      '5.200000',
      '5.295238',
      '1.05',
      'catch-up',
      'true',
    ]);
  });
});
