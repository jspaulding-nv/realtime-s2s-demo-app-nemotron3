import {
  RENDERED_DIGITAL_BLOCK_LEDGER_SCHEMA,
  RENDERED_DIGITAL_CHANNEL_COUNT,
  type RenderedDigitalPcmBlock,
} from '../types/renderedDigitalCapture';

const WAV_HEADER_BYTES = 44;
const PCM_BYTES_PER_SAMPLE = 2;

function writeAscii(
  bytes: Uint8Array,
  offset: number,
  value: string,
): void {
  for (let index = 0; index < value.length; index += 1) {
    bytes[offset + index] = value.charCodeAt(index);
  }
}

export function encodeStereoPcm16Wav(
  interleavedPcm16: Int16Array,
  sampleRateHz: number,
): ArrayBuffer {
  if (
    !Number.isSafeInteger(sampleRateHz)
    || sampleRateHz <= 0
    || interleavedPcm16.length % RENDERED_DIGITAL_CHANNEL_COUNT !== 0
  ) {
    throw new Error('Invalid stereo PCM for rendered-digital WAV export.');
  }

  const dataBytes = interleavedPcm16.byteLength;
  const wav = new ArrayBuffer(WAV_HEADER_BYTES + dataBytes);
  const bytes = new Uint8Array(wav);
  const view = new DataView(wav);
  const blockAlign = (
    RENDERED_DIGITAL_CHANNEL_COUNT * PCM_BYTES_PER_SAMPLE
  );
  writeAscii(bytes, 0, 'RIFF');
  view.setUint32(4, wav.byteLength - 8, true);
  writeAscii(bytes, 8, 'WAVE');
  writeAscii(bytes, 12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, RENDERED_DIGITAL_CHANNEL_COUNT, true);
  view.setUint32(24, sampleRateHz, true);
  view.setUint32(28, sampleRateHz * blockAlign, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, 16, true);
  writeAscii(bytes, 36, 'data');
  view.setUint32(40, dataBytes, true);
  bytes.set(
    new Uint8Array(
      interleavedPcm16.buffer,
      interleavedPcm16.byteOffset,
      interleavedPcm16.byteLength,
    ),
    WAV_HEADER_BYTES,
  );
  return wav;
}

export function deinterleaveStereoPcm16(
  interleavedPcm16: Int16Array,
): {
  source: Int16Array<ArrayBuffer>;
  translated: Int16Array<ArrayBuffer>;
} {
  if (interleavedPcm16.length % RENDERED_DIGITAL_CHANNEL_COUNT !== 0) {
    throw new Error('Rendered-digital PCM does not contain complete frames.');
  }
  const frameCount = (
    interleavedPcm16.length / RENDERED_DIGITAL_CHANNEL_COUNT
  );
  const source = new Int16Array(frameCount);
  const translated = new Int16Array(frameCount);
  for (let frame = 0; frame < frameCount; frame += 1) {
    source[frame] = interleavedPcm16[frame * 2];
    translated[frame] = interleavedPcm16[frame * 2 + 1];
  }
  return { source, translated };
}

export function countNonzeroSamples(pcm16: Int16Array): number {
  let count = 0;
  for (const sample of pcm16) {
    if (sample !== 0) count += 1;
  }
  return count;
}

export function serializeRenderedDigitalBlockLedger(
  blocks: RenderedDigitalPcmBlock[],
): string {
  const rows = [
    [
      'schema',
      'sequence',
      'start_context_frame',
      'capture_frame_start',
      'frame_count',
      'interleaved_pcm_sha256',
    ].join(','),
  ];
  if (blocks.length === 0) {
    throw new Error('Cannot serialize an empty rendered-digital ledger.');
  }
  const captureStart = blocks[0].startContextFrame;
  for (const block of blocks) {
    rows.push([
      RENDERED_DIGITAL_BLOCK_LEDGER_SCHEMA,
      block.sequence,
      block.startContextFrame,
      block.startContextFrame - captureStart,
      block.frameCount,
      block.interleavedPcmSha256,
    ].join(','));
  }
  return `${rows.join('\n')}\n`;
}

export function downloadPrivateArtifact(
  data: BlobPart,
  mimeType: string,
  filename: string,
): void {
  const blob = new Blob([data], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  setTimeout(() => {
    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
  }, 100);
}
