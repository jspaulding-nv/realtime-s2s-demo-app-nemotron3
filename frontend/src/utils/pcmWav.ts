export interface ExactPcmWav {
  pcmBytes: Uint8Array<ArrayBuffer>;
  sampleCount: number;
}

const RIFF_HEADER_BYTES = 12;
const CHUNK_HEADER_BYTES = 8;
const PCM_FORMAT = 1;
const PCM16_BYTES_PER_SAMPLE = 2;

function hasFourCc(
  bytes: Uint8Array,
  offset: number,
  first: number,
  second: number,
  third: number,
  fourth: number,
): boolean {
  return (
    bytes[offset] === first
    && bytes[offset + 1] === second
    && bytes[offset + 2] === third
    && bytes[offset + 3] === fourth
  );
}

/**
 * Return the byte-exact sample payload only when a RIFF/WAVE file already
 * matches the ASR wire format. Any malformed, ambiguous, or unsupported file
 * returns null so the caller can use its normal decoder/resampler.
 */
export function extractExactMonoPcm16Wav(
  buffer: ArrayBuffer,
  targetSampleRate: number,
): ExactPcmWav | null {
  if (
    !Number.isSafeInteger(targetSampleRate)
    || targetSampleRate <= 0
    || buffer.byteLength < RIFF_HEADER_BYTES
  ) {
    return null;
  }

  const bytes = new Uint8Array<ArrayBuffer>(buffer);
  if (
    !hasFourCc(bytes, 0, 0x52, 0x49, 0x46, 0x46) // RIFF
    || !hasFourCc(bytes, 8, 0x57, 0x41, 0x56, 0x45) // WAVE
  ) {
    return null;
  }

  const view = new DataView(buffer);
  const riffEnd = view.getUint32(4, true) + 8;
  // A strict passthrough must not silently omit a truncated chunk or include
  // bytes outside the declared RIFF form.
  if (riffEnd !== buffer.byteLength) {
    return null;
  }

  let offset = RIFF_HEADER_BYTES;
  let format:
    | {
      audioFormat: number;
      channels: number;
      sampleRate: number;
      byteRate: number;
      blockAlign: number;
      bitsPerSample: number;
    }
    | null = null;
  let dataOffset: number | null = null;
  let dataLength: number | null = null;

  while (offset < riffEnd) {
    if (riffEnd - offset < CHUNK_HEADER_BYTES) {
      return null;
    }

    const chunkLength = view.getUint32(offset + 4, true);
    const payloadOffset = offset + CHUNK_HEADER_BYTES;
    const payloadEnd = payloadOffset + chunkLength;
    const paddedEnd = payloadEnd + (chunkLength & 1);
    if (payloadEnd > riffEnd || paddedEnd > riffEnd) {
      return null;
    }

    if (hasFourCc(bytes, offset, 0x66, 0x6d, 0x74, 0x20)) { // fmt
      if (format !== null || chunkLength < 16) {
        return null;
      }
      format = {
        audioFormat: view.getUint16(payloadOffset, true),
        channels: view.getUint16(payloadOffset + 2, true),
        sampleRate: view.getUint32(payloadOffset + 4, true),
        byteRate: view.getUint32(payloadOffset + 8, true),
        blockAlign: view.getUint16(payloadOffset + 12, true),
        bitsPerSample: view.getUint16(payloadOffset + 14, true),
      };
    } else if (
      hasFourCc(bytes, offset, 0x64, 0x61, 0x74, 0x61) // data
    ) {
      if (dataOffset !== null) {
        return null;
      }
      dataOffset = payloadOffset;
      dataLength = chunkLength;
    }

    offset = paddedEnd;
  }

  if (format === null || dataOffset === null || dataLength === null) {
    return null;
  }

  const expectedBlockAlign = PCM16_BYTES_PER_SAMPLE;
  if (
    format.audioFormat !== PCM_FORMAT
    || format.channels !== 1
    || format.sampleRate !== targetSampleRate
    || format.bitsPerSample !== 16
    || format.blockAlign !== expectedBlockAlign
    || format.byteRate !== targetSampleRate * expectedBlockAlign
    || dataLength % format.blockAlign !== 0
  ) {
    return null;
  }

  return {
    pcmBytes: bytes.subarray(dataOffset, dataOffset + dataLength),
    sampleCount: dataLength / format.blockAlign,
  };
}
