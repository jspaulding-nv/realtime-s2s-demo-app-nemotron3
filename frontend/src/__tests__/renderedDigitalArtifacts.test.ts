import { describe, expect, it } from 'vitest';
import {
  deinterleaveStereoPcm16,
  encodeStereoPcm16Wav,
  serializeRenderedDigitalBlockLedger,
} from '../utils/renderedDigitalArtifacts';

describe('rendered-digital artifacts', () => {
  it('writes the exact strict stereo PCM16 RIFF layout', () => {
    const pcm = Int16Array.of(1, -1, 32767, -32768);
    const wav = encodeStereoPcm16Wav(pcm, 16000);
    const bytes = new Uint8Array(wav);
    const view = new DataView(wav);

    expect(new TextDecoder().decode(bytes.slice(0, 4))).toBe('RIFF');
    expect(view.getUint32(4, true)).toBe(44);
    expect(new TextDecoder().decode(bytes.slice(8, 12))).toBe('WAVE');
    expect(new TextDecoder().decode(bytes.slice(12, 16))).toBe('fmt ');
    expect(view.getUint32(16, true)).toBe(16);
    expect(view.getUint16(20, true)).toBe(1);
    expect(view.getUint16(22, true)).toBe(2);
    expect(view.getUint32(24, true)).toBe(16000);
    expect(view.getUint32(28, true)).toBe(64000);
    expect(view.getUint16(32, true)).toBe(4);
    expect(view.getUint16(34, true)).toBe(16);
    expect(new TextDecoder().decode(bytes.slice(36, 40))).toBe('data');
    expect(view.getUint32(40, true)).toBe(8);
    expect(Array.from(new Int16Array(wav, 44))).toEqual(
      [1, -1, 32767, -32768],
    );
  });

  it('deinterleaves channel zero and channel one without reordering', () => {
    const channels = deinterleaveStereoPcm16(
      Int16Array.of(10, 20, 11, 21, 12, 22),
    );
    expect(Array.from(channels.source)).toEqual([10, 11, 12]);
    expect(Array.from(channels.translated)).toEqual([20, 21, 22]);
  });

  it('rejects an incomplete stereo frame', () => {
    expect(() => encodeStereoPcm16Wav(Int16Array.of(1), 16000))
      .toThrow(/Invalid stereo PCM/);
    expect(() => deinterleaveStereoPcm16(Int16Array.of(1)))
      .toThrow(/complete frames/);
  });

  it('serializes a contiguous, capture-relative block ledger', () => {
    const csv = serializeRenderedDigitalBlockLedger([
      {
        sequence: 0,
        startContextFrame: 1024,
        frameCount: 2,
        interleavedPcm16: Int16Array.of(1, 2, 3, 4),
        interleavedPcmSha256: 'a'.repeat(64),
      },
      {
        sequence: 1,
        startContextFrame: 1026,
        frameCount: 1,
        interleavedPcm16: Int16Array.of(5, 6),
        interleavedPcmSha256: 'b'.repeat(64),
      },
    ]);
    expect(csv.split('\n')).toEqual([
      'schema,sequence,start_context_frame,capture_frame_start,frame_count,interleaved_pcm_sha256',
      `rendered-digital-block-ledger/v1,0,1024,0,2,${'a'.repeat(64)}`,
      `rendered-digital-block-ledger/v1,1,1026,2,1,${'b'.repeat(64)}`,
      '',
    ]);
  });
});
