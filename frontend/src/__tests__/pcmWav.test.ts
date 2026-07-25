import { describe, expect, it } from 'vitest';
import { extractExactMonoPcm16Wav } from '../utils/pcmWav';
import {
  pcm16LeBytes,
  riffChunk,
  riffWave,
  waveFormatChunk,
} from './wavTestFixtures';

describe('extractExactMonoPcm16Wav', () => {
  it('extracts exact little-endian bytes across odd chunks and fmt extensions', () => {
    const samples = [0x1234, -2, -32768, 32767, 1];
    const expectedBytes = pcm16LeBytes(samples);
    const wav = riffWave([
      riffChunk('JUNK', Uint8Array.of(0xaa, 0xbb, 0xcc), 0x7f),
      waveFormatChunk({ extension: Uint8Array.of(0, 0) }),
      riffChunk('LIST', Uint8Array.of(1, 2, 3, 4, 5), 0xee),
      riffChunk('data', expectedBytes),
    ]);

    const parsed = extractExactMonoPcm16Wav(wav.buffer, 16000);

    expect(parsed?.sampleCount).toBe(samples.length);
    expect(Array.from(parsed?.pcmBytes ?? [])).toEqual(
      Array.from(expectedBytes),
    );
  });

  it.each([
    ['non-PCM format', { audioFormat: 3 }],
    ['multiple channels', { channels: 2 }],
    ['different sample rate', { sampleRate: 48000 }],
    ['different sample width', { bitsPerSample: 24 }],
    ['invalid block alignment', { blockAlign: 4 }],
    ['invalid byte rate', { byteRate: 1 }],
  ])('does not pass through %s', (_label, format) => {
    const wav = riffWave([
      waveFormatChunk(format),
      riffChunk('data', pcm16LeBytes([1, 2])),
    ]);

    expect(extractExactMonoPcm16Wav(wav.buffer, 16000)).toBeNull();
  });

  it('rejects a data payload that is not block aligned', () => {
    const wav = riffWave([
      waveFormatChunk(),
      riffChunk('data', Uint8Array.of(1, 2, 3)),
    ]);

    expect(extractExactMonoPcm16Wav(wav.buffer, 16000)).toBeNull();
  });

  it('rejects duplicate format or data chunks as ambiguous', () => {
    const format = waveFormatChunk();
    const data = riffChunk('data', pcm16LeBytes([1]));

    expect(
      extractExactMonoPcm16Wav(
        riffWave([format, format, data]).buffer,
        16000,
      ),
    ).toBeNull();
    expect(
      extractExactMonoPcm16Wav(
        riffWave([format, data, data]).buffer,
        16000,
      ),
    ).toBeNull();
  });

  it.each([
    ['truncated RIFF', (wav: Uint8Array) => wav.slice(0, -1)],
    ['undersized RIFF declaration', (wav: Uint8Array) => {
      const copy = Uint8Array.from(wav);
      new DataView(copy.buffer).setUint32(4, copy.byteLength - 9, true);
      return copy;
    }],
    ['oversized chunk declaration', (wav: Uint8Array) => {
      const copy = Uint8Array.from(wav);
      new DataView(copy.buffer).setUint32(16, 0xffffffff, true);
      return copy;
    }],
  ])('rejects malformed input: %s', (_label, mutate) => {
    const valid = riffWave([
      waveFormatChunk(),
      riffChunk('data', pcm16LeBytes([1, 2])),
    ]);
    const malformed = mutate(valid);

    expect(
      extractExactMonoPcm16Wav(malformed.buffer, 16000),
    ).toBeNull();
  });
});
