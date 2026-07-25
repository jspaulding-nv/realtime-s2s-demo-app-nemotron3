import { describe, expect, it } from 'vitest';
import type {
  AudioFrameMetadata,
  AudioParentCompleteMetadata,
} from '../types/audioMetadata';
import {
  AudioMetadataProtocolError,
  AudioMetadataProtocolV1Receiver,
  parseAudioMetadataMessageV1,
} from '../utils/audioMetadataProtocol';

function frame(
  overrides: Partial<AudioFrameMetadata> = {},
): AudioFrameMetadata {
  return {
    type: 'audio_frame',
    protocolVersion: 1,
    streamGeneration: 1,
    parentSequenceId: 0,
    audioFrameId: 0,
    audioBytes: 4,
    sampleRateHz: 16000,
    channels: 1,
    bytesPerSample: 2,
    sourceStartMs: 100,
    sourceEndMs: 500,
    ...overrides,
  };
}

function completion(
  overrides: Partial<AudioParentCompleteMetadata> = {},
): AudioParentCompleteMetadata {
  return {
    type: 'audio_parent_complete',
    protocolVersion: 1,
    streamGeneration: 1,
    parentSequenceId: 0,
    audioFrameCount: 1,
    audioBytes: 4,
    sourceStartMs: 100,
    sourceEndMs: 500,
    ...overrides,
  };
}

function acceptFrame(
  receiver: AudioMetadataProtocolV1Receiver,
  metadata: AudioFrameMetadata,
): void {
  receiver.acceptMetadataMessage(metadata);
  receiver.acceptBinary(new ArrayBuffer(metadata.audioBytes));
}

describe('audio metadata protocol v1 parser', () => {
  it('accepts exact frame and completion shapes, including null source ranges', () => {
    expect(parseAudioMetadataMessageV1(frame())).toEqual(frame());
    expect(
      parseAudioMetadataMessageV1(completion({
        sourceStartMs: null,
        sourceEndMs: null,
      })),
    ).toEqual(completion({
      sourceStartMs: null,
      sourceEndMs: null,
    }));
  });

  it('rejects extra fields, unsupported versions, and invalid source ranges', () => {
    expect(() => parseAudioMetadataMessageV1({
      ...frame(),
      text: 'must not cross the observation boundary',
    })).toThrow(/exactly the version 1 fields/);
    expect(() => parseAudioMetadataMessageV1({
      ...frame(),
      protocolVersion: 2,
    })).toThrow(/protocolVersion/);
    expect(parseAudioMetadataMessageV1({
      ...frame(),
      sourceStartMs: null,
    })).toEqual(frame({ sourceStartMs: null }));
    expect(() => parseAudioMetadataMessageV1({
      ...frame(),
      sourceStartMs: -1,
    })).toThrow(/sourceStartMs/);
    expect(() => parseAudioMetadataMessageV1({
      ...frame(),
      sourceStartMs: 600,
    })).toThrow(/cannot precede/);
  });

  it('rejects unsafe identities and PCM byte misalignment', () => {
    expect(() => parseAudioMetadataMessageV1({
      ...frame(),
      streamGeneration: 0,
    })).toThrow(/positive safe integer/);
    expect(() => parseAudioMetadataMessageV1({
      ...frame(),
      parentSequenceId: -1,
    })).toThrow(/non-negative safe integer/);
    expect(() => parseAudioMetadataMessageV1({
      ...frame(),
      audioBytes: 3,
    })).toThrow(/complete PCM sample/);
  });
});

describe('AudioMetadataProtocolV1Receiver', () => {
  it('pairs contiguous frames and reconciles each parent completion', () => {
    const receiver = new AudioMetadataProtocolV1Receiver();
    receiver.beginStream();

    acceptFrame(receiver, frame());
    acceptFrame(receiver, frame({
      audioFrameId: 1,
      audioBytes: 6,
    }));
    expect(receiver.acceptMetadataMessage(completion({
      audioFrameCount: 2,
      audioBytes: 10,
    }))).toEqual(completion({
      audioFrameCount: 2,
      audioBytes: 10,
    }));

    acceptFrame(receiver, frame({
      parentSequenceId: 1,
      audioFrameId: 0,
      sourceStartMs: null,
      sourceEndMs: null,
    }));
    receiver.acceptMetadataMessage(completion({
      parentSequenceId: 1,
      sourceStartMs: null,
      sourceEndMs: null,
    }));

    expect(() => receiver.finishStream('completed stream')).not.toThrow();
  });

  it('requires exactly one header immediately before exact-length PCM', () => {
    const duplicateHeader = new AudioMetadataProtocolV1Receiver();
    duplicateHeader.beginStream();
    duplicateHeader.acceptMetadataMessage(frame());
    expect(() => duplicateHeader.acceptMetadataMessage(frame())).toThrow(
      /before another header/,
    );

    const missingHeader = new AudioMetadataProtocolV1Receiver();
    missingHeader.beginStream();
    expect(() => missingHeader.acceptBinary(new ArrayBuffer(4))).toThrow(
      /preceding audio_frame header/,
    );

    const wrongLength = new AudioMetadataProtocolV1Receiver();
    wrongLength.beginStream();
    wrongLength.acceptMetadataMessage(frame());
    expect(() => wrongLength.acceptBinary(new ArrayBuffer(2))).toThrow(
      /does not match audioBytes/,
    );

    const interleavedControl = new AudioMetadataProtocolV1Receiver();
    interleavedControl.beginStream();
    interleavedControl.acceptMetadataMessage(frame());
    expect(() => interleavedControl.observeControlMessage()).toThrow(
      /followed immediately/,
    );
  });

  it('latches closed after a violation until an explicit restart', () => {
    const receiver = new AudioMetadataProtocolV1Receiver();
    receiver.beginStream();
    expect(() => receiver.acceptBinary(new ArrayBuffer(4))).toThrow(
      AudioMetadataProtocolError,
    );
    expect(() => receiver.acceptMetadataMessage(frame())).toThrow(
      /closed after a protocol violation/,
    );

    receiver.beginStream();
    acceptFrame(receiver, frame());
    receiver.acceptMetadataMessage(completion());
    expect(() => receiver.finishStream('completed stream')).not.toThrow();
  });

  it('enforces contiguous parent and frame ordering', () => {
    const skippedFrame = new AudioMetadataProtocolV1Receiver();
    skippedFrame.beginStream();
    acceptFrame(skippedFrame, frame());
    expect(() => skippedFrame.acceptMetadataMessage(frame({
      audioFrameId: 2,
    }))).toThrow(/expected audioFrameId 1/);

    const nextParentBeforeCompletion = new AudioMetadataProtocolV1Receiver();
    nextParentBeforeCompletion.beginStream();
    acceptFrame(nextParentBeforeCompletion, frame());
    expect(() => nextParentBeforeCompletion.acceptMetadataMessage(frame({
      parentSequenceId: 1,
      audioFrameId: 0,
    }))).toThrow(/parent must complete/);

    const skippedParent = new AudioMetadataProtocolV1Receiver();
    skippedParent.beginStream();
    expect(() => skippedParent.acceptMetadataMessage(frame({
      parentSequenceId: 1,
    }))).toThrow(/expected parentSequenceId 0/);
  });

  it('enforces stable PCM format, generation, and parent source range', () => {
    const format = new AudioMetadataProtocolV1Receiver();
    format.beginStream();
    acceptFrame(format, frame());
    expect(() => format.acceptMetadataMessage(frame({
      audioFrameId: 1,
      sampleRateHz: 24000,
    }))).toThrow(/PCM format must remain stable/);

    const generation = new AudioMetadataProtocolV1Receiver();
    generation.beginStream();
    acceptFrame(generation, frame());
    expect(() => generation.acceptMetadataMessage(frame({
      streamGeneration: 2,
      audioFrameId: 1,
    }))).toThrow(/changed within a stream/);

    const sourceRange = new AudioMetadataProtocolV1Receiver();
    sourceRange.beginStream();
    acceptFrame(sourceRange, frame());
    expect(() => sourceRange.acceptMetadataMessage(frame({
      audioFrameId: 1,
      sourceEndMs: 501,
    }))).toThrow(/source range must remain stable/);
  });

  it('rejects completion totals, identity, and source-range mismatches', () => {
    const cases: Array<{
      metadata: Partial<AudioParentCompleteMetadata>;
      message: RegExp;
    }> = [
      {
        metadata: { audioFrameCount: 2 },
        message: /does not match observed frame count/,
      },
      {
        metadata: { audioBytes: 6 },
        message: /does not match observed parent bytes/,
      },
      {
        metadata: { parentSequenceId: 1 },
        message: /does not match active parent/,
      },
      {
        metadata: { sourceEndMs: 501 },
        message: /source range does not match/,
      },
    ];

    for (const testCase of cases) {
      const receiver = new AudioMetadataProtocolV1Receiver();
      receiver.beginStream();
      acceptFrame(receiver, frame());
      expect(() => receiver.acceptMetadataMessage(
        completion(testCase.metadata),
      )).toThrow(testCase.message);
    }
  });

  it('requires generations to increase across same-connection restarts', () => {
    const receiver = new AudioMetadataProtocolV1Receiver();
    receiver.beginStream();
    acceptFrame(receiver, frame({ streamGeneration: 3 }));
    receiver.acceptMetadataMessage(completion({ streamGeneration: 3 }));
    receiver.finishStream('completed stream');

    receiver.beginStream();
    expect(() => receiver.acceptMetadataMessage(frame({
      streamGeneration: 3,
    }))).toThrow(/must increase after restart/);

    receiver.beginStream();
    acceptFrame(receiver, frame({ streamGeneration: 5 }));
  });

  it('reports dangling headers and parents while clearing restart state', () => {
    const danglingHeader = new AudioMetadataProtocolV1Receiver();
    danglingHeader.beginStream();
    danglingHeader.acceptMetadataMessage(frame());
    expect(() => danglingHeader.beginStream()).toThrow(
      /header without binary PCM/,
    );
    acceptFrame(danglingHeader, frame({ streamGeneration: 2 }));

    const danglingParent = new AudioMetadataProtocolV1Receiver();
    danglingParent.beginStream();
    acceptFrame(danglingParent, frame());
    expect(() => danglingParent.finishStream('WebSocket disconnect')).toThrow(
      /without audio_parent_complete/,
    );
    danglingParent.beginStream();
    acceptFrame(danglingParent, frame({ streamGeneration: 2 }));
  });
});
