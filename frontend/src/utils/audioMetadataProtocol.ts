import {
  AUDIO_METADATA_PROTOCOL_VERSION,
  type AudioFrameMetadata,
  type AudioMetadataMessage,
  type AudioParentCompleteMetadata,
  type AudioSourceRange,
} from '../types/audioMetadata';

const AUDIO_FRAME_KEYS = [
  'type',
  'protocolVersion',
  'streamGeneration',
  'parentSequenceId',
  'audioFrameId',
  'audioBytes',
  'sampleRateHz',
  'channels',
  'bytesPerSample',
  'sourceStartMs',
  'sourceEndMs',
] as const;

const AUDIO_PARENT_COMPLETE_KEYS = [
  'type',
  'protocolVersion',
  'streamGeneration',
  'parentSequenceId',
  'audioFrameCount',
  'audioBytes',
  'sourceStartMs',
  'sourceEndMs',
] as const;

interface PcmFormat {
  sampleRateHz: number;
  channels: number;
  bytesPerSample: number;
}

interface ParentAccumulator extends AudioSourceRange {
  parentSequenceId: number;
  audioFrameCount: number;
  audioBytes: number;
}

export class AudioMetadataProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'AudioMetadataProtocolError';
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function assertExactKeys(
  value: Record<string, unknown>,
  expectedKeys: readonly string[],
  messageType: string,
): void {
  const actualKeys = Object.keys(value).sort();
  const sortedExpectedKeys = [...expectedKeys].sort();

  if (
    actualKeys.length !== sortedExpectedKeys.length
    || actualKeys.some((key, index) => key !== sortedExpectedKeys[index])
  ) {
    throw new AudioMetadataProtocolError(
      `${messageType} must contain exactly the version 1 fields`,
    );
  }
}

function requirePositiveSafeInteger(
  value: unknown,
  fieldName: string,
): number {
  if (!Number.isSafeInteger(value) || (value as number) <= 0) {
    throw new AudioMetadataProtocolError(
      `${fieldName} must be a positive safe integer`,
    );
  }
  return value as number;
}

function requireNonnegativeSafeInteger(
  value: unknown,
  fieldName: string,
): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new AudioMetadataProtocolError(
      `${fieldName} must be a non-negative safe integer`,
    );
  }
  return value as number;
}

function parseSourceRange(value: Record<string, unknown>): AudioSourceRange {
  const start = value.sourceStartMs;
  const end = value.sourceEndMs;

  if (
    start !== null
    && (
      typeof start !== 'number'
      || !Number.isFinite(start)
      || start < 0
    )
  ) {
    throw new AudioMetadataProtocolError(
      'sourceStartMs must be null or a finite non-negative number',
    );
  }
  if (
    end !== null
    && (
      typeof end !== 'number'
      || !Number.isFinite(end)
      || end < 0
    )
  ) {
    throw new AudioMetadataProtocolError(
      'sourceEndMs must be null or a finite non-negative number',
    );
  }
  if (
    typeof start === 'number'
    && typeof end === 'number'
    && end < start
  ) {
    throw new AudioMetadataProtocolError(
      'sourceEndMs cannot precede sourceStartMs',
    );
  }

  return {
    sourceStartMs: start as number | null,
    sourceEndMs: end as number | null,
  };
}

function assertProtocolVersion(value: unknown): void {
  if (value !== AUDIO_METADATA_PROTOCOL_VERSION) {
    throw new AudioMetadataProtocolError(
      `protocolVersion must equal ${AUDIO_METADATA_PROTOCOL_VERSION}`,
    );
  }
}

function parseAudioFrameMetadata(
  value: Record<string, unknown>,
): AudioFrameMetadata {
  assertExactKeys(value, AUDIO_FRAME_KEYS, 'audio_frame');
  assertProtocolVersion(value.protocolVersion);

  const metadata: AudioFrameMetadata = {
    type: 'audio_frame',
    protocolVersion: AUDIO_METADATA_PROTOCOL_VERSION,
    streamGeneration: requirePositiveSafeInteger(
      value.streamGeneration,
      'streamGeneration',
    ),
    parentSequenceId: requireNonnegativeSafeInteger(
      value.parentSequenceId,
      'parentSequenceId',
    ),
    audioFrameId: requireNonnegativeSafeInteger(
      value.audioFrameId,
      'audioFrameId',
    ),
    audioBytes: requirePositiveSafeInteger(value.audioBytes, 'audioBytes'),
    sampleRateHz: requirePositiveSafeInteger(
      value.sampleRateHz,
      'sampleRateHz',
    ),
    channels: requirePositiveSafeInteger(value.channels, 'channels'),
    bytesPerSample: requirePositiveSafeInteger(
      value.bytesPerSample,
      'bytesPerSample',
    ),
    ...parseSourceRange(value),
  };

  if (
    metadata.audioBytes
    % (metadata.channels * metadata.bytesPerSample)
    !== 0
  ) {
    throw new AudioMetadataProtocolError(
      'audioBytes must align to a complete PCM sample',
    );
  }

  return metadata;
}

function parseAudioParentCompleteMetadata(
  value: Record<string, unknown>,
): AudioParentCompleteMetadata {
  assertExactKeys(
    value,
    AUDIO_PARENT_COMPLETE_KEYS,
    'audio_parent_complete',
  );
  assertProtocolVersion(value.protocolVersion);

  return {
    type: 'audio_parent_complete',
    protocolVersion: AUDIO_METADATA_PROTOCOL_VERSION,
    streamGeneration: requirePositiveSafeInteger(
      value.streamGeneration,
      'streamGeneration',
    ),
    parentSequenceId: requireNonnegativeSafeInteger(
      value.parentSequenceId,
      'parentSequenceId',
    ),
    audioFrameCount: requirePositiveSafeInteger(
      value.audioFrameCount,
      'audioFrameCount',
    ),
    audioBytes: requirePositiveSafeInteger(value.audioBytes, 'audioBytes'),
    ...parseSourceRange(value),
  };
}

export function parseAudioMetadataMessageV1(
  value: unknown,
): AudioMetadataMessage {
  if (!isRecord(value)) {
    throw new AudioMetadataProtocolError(
      'audio metadata message must be a JSON object',
    );
  }

  if (value.type === 'audio_frame') {
    return parseAudioFrameMetadata(value);
  }
  if (value.type === 'audio_parent_complete') {
    return parseAudioParentCompleteMetadata(value);
  }

  throw new AudioMetadataProtocolError(
    'audio metadata message has an unsupported type',
  );
}

function sourceRangesEqual(
  left: AudioSourceRange,
  right: AudioSourceRange,
): boolean {
  return (
    left.sourceStartMs === right.sourceStartMs
    && left.sourceEndMs === right.sourceEndMs
  );
}

function pcmFormatsEqual(left: PcmFormat, right: PcmFormat): boolean {
  return (
    left.sampleRateHz === right.sampleRateHz
    && left.channels === right.channels
    && left.bytesPerSample === right.bytesPerSample
  );
}

/**
 * Strict receiver for the opt-in audio metadata protocol.
 *
 * It observes identity only. It never buffers, rewrites, reschedules, or drops
 * PCM. A protocol violation latches the receiver closed until an explicit
 * stream restart or connection reset.
 */
export class AudioMetadataProtocolV1Receiver {
  private active = false;
  private failed = false;
  private pendingFrame: AudioFrameMetadata | null = null;
  private streamGeneration: number | null = null;
  private lastStreamGeneration: number | null = null;
  private pcmFormat: PcmFormat | null = null;
  private currentParent: ParentAccumulator | null = null;
  private expectedParentSequenceId = 0;

  beginStream(): void {
    const danglingError = this.danglingError('stream restart');
    this.rememberCurrentGeneration();
    this.clearStreamState();
    this.active = true;

    if (danglingError) {
      throw danglingError;
    }
  }

  finishStream(context: string): void {
    const danglingError = this.danglingError(context);
    this.rememberCurrentGeneration();
    this.clearStreamState();

    if (danglingError) {
      throw danglingError;
    }
  }

  observeControlMessage(): void {
    this.ensureUsable();
    if (this.pendingFrame) {
      this.raise(
        'audio_frame header must be followed immediately by its binary PCM',
      );
    }
  }

  closeAfterProtocolViolation(): void {
    this.failed = true;
  }

  acceptMetadataMessage(
    value: unknown,
  ): AudioFrameMetadata | AudioParentCompleteMetadata {
    this.ensureUsable();
    if (!this.active) {
      this.raise('audio metadata arrived outside an active stream');
    }

    let metadata: AudioMetadataMessage;
    try {
      metadata = parseAudioMetadataMessageV1(value);
    } catch (error) {
      this.failed = true;
      throw error;
    }

    if (metadata.type === 'audio_frame') {
      this.acceptFrameHeader(metadata);
    } else {
      this.acceptParentCompletion(metadata);
    }
    return metadata;
  }

  acceptBinary(audio: ArrayBuffer): AudioFrameMetadata {
    this.ensureUsable();
    if (!this.active) {
      this.raise('binary PCM arrived outside an active stream');
    }
    if (!this.pendingFrame) {
      this.raise('binary PCM must have exactly one preceding audio_frame header');
    }

    const metadata = this.pendingFrame;
    if (audio.byteLength !== metadata.audioBytes) {
      this.raise(
        `binary PCM byte count ${audio.byteLength} does not match audioBytes ${metadata.audioBytes}`,
      );
    }
    if (!this.currentParent) {
      this.raise('audio_frame parent state is missing');
    }

    this.currentParent.audioFrameCount += 1;
    this.currentParent.audioBytes += audio.byteLength;
    this.pendingFrame = null;
    return metadata;
  }

  private acceptFrameHeader(metadata: AudioFrameMetadata): void {
    if (this.pendingFrame) {
      this.raise(
        'audio_frame header must be followed by binary PCM before another header',
      );
    }

    this.acceptGeneration(metadata.streamGeneration);
    this.acceptPcmFormat(metadata);

    if (!this.currentParent) {
      if (metadata.parentSequenceId !== this.expectedParentSequenceId) {
        this.raise(
          `expected parentSequenceId ${this.expectedParentSequenceId}, received ${metadata.parentSequenceId}`,
        );
      }
      if (metadata.audioFrameId !== 0) {
        this.raise('the first audio frame in a parent must have audioFrameId 0');
      }
      this.currentParent = {
        parentSequenceId: metadata.parentSequenceId,
        audioFrameCount: 0,
        audioBytes: 0,
        sourceStartMs: metadata.sourceStartMs,
        sourceEndMs: metadata.sourceEndMs,
      };
    } else {
      if (metadata.parentSequenceId !== this.currentParent.parentSequenceId) {
        this.raise(
          'a parent must complete before frames for the next parent arrive',
        );
      }
      if (metadata.audioFrameId !== this.currentParent.audioFrameCount) {
        this.raise(
          `expected audioFrameId ${this.currentParent.audioFrameCount}, received ${metadata.audioFrameId}`,
        );
      }
      if (!sourceRangesEqual(metadata, this.currentParent)) {
        this.raise('source range must remain stable within a parent');
      }
    }

    this.pendingFrame = metadata;
  }

  private acceptParentCompletion(
    metadata: AudioParentCompleteMetadata,
  ): void {
    if (this.pendingFrame) {
      this.raise(
        'audio_frame header must be followed by binary PCM before parent completion',
      );
    }
    if (!this.currentParent) {
      this.raise('audio_parent_complete must follow at least one audio frame');
    }

    this.acceptGeneration(metadata.streamGeneration);

    if (metadata.parentSequenceId !== this.currentParent.parentSequenceId) {
      this.raise(
        `audio_parent_complete parentSequenceId ${metadata.parentSequenceId} does not match active parent ${this.currentParent.parentSequenceId}`,
      );
    }
    if (metadata.audioFrameCount !== this.currentParent.audioFrameCount) {
      this.raise(
        `audioFrameCount ${metadata.audioFrameCount} does not match observed frame count ${this.currentParent.audioFrameCount}`,
      );
    }
    if (metadata.audioBytes !== this.currentParent.audioBytes) {
      this.raise(
        `audioBytes ${metadata.audioBytes} does not match observed parent bytes ${this.currentParent.audioBytes}`,
      );
    }
    if (!sourceRangesEqual(metadata, this.currentParent)) {
      this.raise(
        'audio_parent_complete source range does not match its frames',
      );
    }

    this.expectedParentSequenceId += 1;
    this.currentParent = null;
  }

  private acceptGeneration(generation: number): void {
    if (this.streamGeneration === null) {
      if (
        this.lastStreamGeneration !== null
        && generation <= this.lastStreamGeneration
      ) {
        this.raise(
          `streamGeneration must increase after restart; previous ${this.lastStreamGeneration}, received ${generation}`,
        );
      }
      this.streamGeneration = generation;
      return;
    }

    if (generation !== this.streamGeneration) {
      this.raise(
        `streamGeneration changed within a stream from ${this.streamGeneration} to ${generation}`,
      );
    }
  }

  private acceptPcmFormat(metadata: AudioFrameMetadata): void {
    const format: PcmFormat = {
      sampleRateHz: metadata.sampleRateHz,
      channels: metadata.channels,
      bytesPerSample: metadata.bytesPerSample,
    };
    if (!this.pcmFormat) {
      this.pcmFormat = format;
      return;
    }
    if (!pcmFormatsEqual(format, this.pcmFormat)) {
      this.raise('PCM format must remain stable within a stream');
    }
  }

  private danglingError(context: string): AudioMetadataProtocolError | null {
    if (!this.active || this.failed) {
      return null;
    }
    if (this.pendingFrame) {
      return new AudioMetadataProtocolError(
        `${context} left an audio_frame header without binary PCM`,
      );
    }
    if (this.currentParent) {
      return new AudioMetadataProtocolError(
        `${context} left parentSequenceId ${this.currentParent.parentSequenceId} without audio_parent_complete`,
      );
    }
    return null;
  }

  private rememberCurrentGeneration(): void {
    if (this.streamGeneration !== null) {
      this.lastStreamGeneration = this.streamGeneration;
    }
  }

  private clearStreamState(): void {
    this.active = false;
    this.failed = false;
    this.pendingFrame = null;
    this.streamGeneration = null;
    this.pcmFormat = null;
    this.currentParent = null;
    this.expectedParentSequenceId = 0;
  }

  private ensureUsable(): void {
    if (this.failed) {
      throw new AudioMetadataProtocolError(
        'audio metadata receiver is closed after a protocol violation',
      );
    }
  }

  private raise(message: string): never {
    this.failed = true;
    throw new AudioMetadataProtocolError(message);
  }
}
