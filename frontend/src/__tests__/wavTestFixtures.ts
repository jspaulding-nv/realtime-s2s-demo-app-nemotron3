export interface WaveFormatFields {
  audioFormat?: number;
  channels?: number;
  sampleRate?: number;
  byteRate?: number;
  blockAlign?: number;
  bitsPerSample?: number;
  extension?: Uint8Array<ArrayBuffer>;
}

function ascii(value: string): Uint8Array<ArrayBuffer> {
  return Uint8Array.from(Array.from(value, (character) => {
    const codePoint = character.codePointAt(0);
    if (codePoint === undefined || codePoint > 0x7f) {
      throw new Error('test fixture FourCC must be ASCII');
    }
    return codePoint;
  }));
}

export function riffChunk(
  id: string,
  payload: Uint8Array<ArrayBuffer>,
  padByte = 0,
): Uint8Array<ArrayBuffer> {
  if (id.length !== 4) {
    throw new Error('RIFF chunk ID must contain four characters');
  }
  const result = new Uint8Array(
    8 + payload.byteLength + (payload.byteLength & 1),
  );
  result.set(ascii(id), 0);
  new DataView(result.buffer).setUint32(4, payload.byteLength, true);
  result.set(payload, 8);
  if ((payload.byteLength & 1) !== 0) {
    result[result.length - 1] = padByte;
  }
  return result;
}

export function waveFormatChunk({
  audioFormat = 1,
  channels = 1,
  sampleRate = 16000,
  byteRate = sampleRate * channels * 2,
  blockAlign = channels * 2,
  bitsPerSample = 16,
  extension = new Uint8Array(0),
}: WaveFormatFields = {}): Uint8Array<ArrayBuffer> {
  const payload = new Uint8Array(16 + extension.byteLength);
  const view = new DataView(payload.buffer);
  view.setUint16(0, audioFormat, true);
  view.setUint16(2, channels, true);
  view.setUint32(4, sampleRate, true);
  view.setUint32(8, byteRate, true);
  view.setUint16(12, blockAlign, true);
  view.setUint16(14, bitsPerSample, true);
  payload.set(extension, 16);
  return riffChunk('fmt ', payload);
}

export function pcm16LeBytes(
  samples: readonly number[],
): Uint8Array<ArrayBuffer> {
  const bytes = new Uint8Array(samples.length * 2);
  const view = new DataView(bytes.buffer);
  samples.forEach((sample, index) => {
    view.setInt16(index * 2, sample, true);
  });
  return bytes;
}

export function riffWave(
  chunks: readonly Uint8Array<ArrayBuffer>[],
): Uint8Array<ArrayBuffer> {
  const chunkBytes = chunks.reduce(
    (total, chunk) => total + chunk.byteLength,
    0,
  );
  const result = new Uint8Array(12 + chunkBytes);
  result.set(ascii('RIFF'), 0);
  new DataView(result.buffer).setUint32(4, result.byteLength - 8, true);
  result.set(ascii('WAVE'), 8);
  let offset = 12;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

export function pcm16MonoWave(
  samples: readonly number[],
  extraChunks: readonly Uint8Array<ArrayBuffer>[] = [],
): Uint8Array<ArrayBuffer> {
  return riffWave([
    waveFormatChunk(),
    ...extraChunks,
    riffChunk('data', pcm16LeBytes(samples)),
  ]);
}
