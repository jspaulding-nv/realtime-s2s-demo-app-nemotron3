import { describe, expect, it, vi } from 'vitest';
import type { PlaybackScheduleEvent } from '../hooks/useAudioPlayback';
import type { AudioConfig } from '../types/messages';
import type { RenderedDigitalCaptureResult } from '../types/renderedDigitalCapture';
import {
  deinterleaveStereoPcm16,
  encodeStereoPcm16Wav,
} from '../utils/renderedDigitalArtifacts';
import {
  buildRenderedDigitalManifest,
  type TranslatedTransportEvidence,
} from '../utils/renderedDigitalManifest';
import { sha256Hex } from '../utils/sha256';

vi.mock('../types/renderedDigitalCapture', async (importOriginal) => ({
  ...await importOriginal<
    typeof import('../types/renderedDigitalCapture')
  >(),
  RENDERED_DIGITAL_SOURCE_FRAMES: 3,
  RENDERED_DIGITAL_SOURCE_CHUNK_FRAMES: 1,
  RENDERED_DIGITAL_PREFLIGHT_FILE_SHA256: 'f'.repeat(64),
  RENDERED_DIGITAL_PREFLIGHT_PCM_SHA256:
    '68c865bf2b0b2c9084646464ed9d90f4751015ff892134c479df850cf26a2241',
}));

vi.mock('../utils/frontendBuildProvenance', () => ({
  FRONTEND_BUILD_PROVENANCE: {
    commit: '1'.repeat(40),
    dirty: false,
  },
}));

async function fixture() {
  const captureStart = 100;
  const sourceStart = 102;
  const input = Int16Array.of(101, -102, 103);
  const inputHash = await sha256Hex(input.buffer);
  const interleaved = new Int16Array(20);
  interleaved[2 * 2] = input[0];
  interleaved[3 * 2] = input[1];
  interleaved[4 * 2] = input[2];
  interleaved[6 * 2 + 1] = 900;
  const channels = deinterleaveStereoPcm16(interleaved);
  const wav = encodeStereoPcm16Wav(interleaved, 16000);
  const blockHash = await sha256Hex(interleaved.buffer);
  const capture: RenderedDigitalCaptureResult = {
    sampleRateHz: 16000,
    channelCount: 2,
    captureStartContextFrame: captureStart,
    captureEndContextFrameExclusive: captureStart + 10,
    frameCount: 10,
    blocks: [{
      sequence: 0,
      startContextFrame: captureStart,
      frameCount: 10,
      interleavedPcm16: interleaved,
      interleavedPcmSha256: blockHash,
    }],
    interleavedPcm16: interleaved,
    sourcePcm16: channels.source,
    translatedPcm16: channels.translated,
    wavBytes: wav,
    interleavedPcmSha256: blockHash,
    sourcePcmSha256: await sha256Hex(channels.source.buffer),
    translatedPcmSha256: await sha256Hex(channels.translated.buffer),
    wavSha256: await sha256Hex(wav),
    workletModuleSha256: 'd'.repeat(64),
    sourceNonzeroSampleCount: 3,
    translatedNonzeroSampleCount: 1,
    contextStateViolationCount: 0,
    visibilityViolationCount: 0,
  };
  const sourceSchedule: PlaybackScheduleEvent = {
    playbackClockSessionId: 1,
    timestampMs: 0,
    schedulePerformanceMs: 0,
    audioContextTimeAtScheduleSeconds: 0,
    scheduledStartContextSeconds: sourceStart / 16000,
    scheduledEndContextSeconds: (sourceStart + 3) / 16000,
    scheduledStartContextFrameFloor: sourceStart,
    scheduledEndContextFrameExclusive: sourceStart + 3,
    projectedScheduledStartClientMs: 0,
    audioBytes: input.byteLength,
    sourceDurationSeconds: 3 / 16000,
    scheduledDurationSeconds: 3 / 16000,
    waitBeforePlaybackSeconds: sourceStart / 16000,
    queueDepthSeconds: (sourceStart + 3) / 16000,
    playbackRate: 1,
    playbackMode: 'normal',
    modeChanged: false,
    aboveTarget: false,
    aboveLimit: false,
  };
  const config: AudioConfig = {
    sampleRate: 16000,
    chunkSize: 4800,
    channels: 1,
    pipelineMode: 'staged',
    audioMetadataProtocolVersions: [1],
    repositoryProvenance: {
      commit: '1'.repeat(40),
      dirty: false,
    },
    modelConfig: {
      asr: {
        image: 'registry/asr:1.2.0',
        imageDigest: `sha256:${'a'.repeat(64)}`,
        profile: (
          'name=nemotron-asr-streaming,type=en-US,batch_size=32'
        ),
        eouMs: 800,
        wordTimeOffsets: true,
        sourceLanguage: 'en-US',
      },
      nmt: {
        image: 'registry/nmt:1.5.2',
        imageDigest: `sha256:${'b'.repeat(64)}`,
        profile: null,
        model: 'megatronnmt_any_any_1b',
        sourceLanguage: 'en-US',
        targetLanguage: 'es-US',
      },
      tts: {
        image: 'registry/tts:1.7.0',
        imageDigest: `sha256:${'c'.repeat(64)}`,
        profile: 'name=magpie-tts-multilingual,batch_size=8',
        targetLanguage: 'es-US',
        voice: 'Magpie-Multilingual.ES-US.Isabela',
      },
    },
    stagedConfig: {
      telemetrySchemaVersion: 3,
      segmentMaxChars: 240,
      segmentMaxAgeMs: 2000,
      asrEventQueueMaxSize: 32,
      nmtQueueMaxSize: 4,
      ttsQueueMaxSize: 4,
      outputQueueMaxSize: 4,
      nmtRpcTimeoutSeconds: 15,
      ttsRpcTimeoutSeconds: 60,
      ttsMaxSegmentAudioSeconds: 60,
      ttsMaxRetries: 1,
      ttsResponseChunkTelemetryEnabled: false,
      ttsSubsegmentMaxChars: 0,
      ttsSubsegmentMinChars: 12,
      closeTimeoutSeconds: 10,
      ttsIncrementalPublishEnabled: true,
      ttsIncrementalFrameMs: 500,
      ttsIncrementalAtomicFallbackMaxChars: 4,
    },
  };
  const translatedPcm = Int16Array.of(900).buffer;
  const translatedTransportEvidence: TranslatedTransportEvidence = {
    received: [{
      sequence: 0,
      streamGeneration: 1,
      parentSequenceId: 0,
      audioFrameId: 0,
      sampleRateHz: 16000,
      channels: 1,
      bytesPerSample: 2,
      pcm: translatedPcm.slice(0),
    }],
    scheduled: [{
      sequence: 0,
      streamGeneration: 1,
      parentSequenceId: 0,
      audioFrameId: 0,
      sampleRateHz: 16000,
      channels: 1,
      bytesPerSample: 2,
      pcm: translatedPcm.slice(0),
    }],
  };
  const clientEvents = [
    ...Array.from({ length: 3 }, (_, index) => ({
      stage: 'chunk_sent',
      timestamp: index + 0.1,
      chunkIndex: index,
      sourcePositionSec: index / 16000,
      audioBytes: 2,
      inputChunkEmittedClientMs: index + 1000,
      inputSourceSampleStart: index,
      inputSourceSampleEndExclusive: index + 1,
      inputSampleRateHz: 16000,
      inputPcmSha256: inputHash,
      inputPcmSampleCount: 3,
      inputLedgerValid: true,
      inputSourceBoundaryContextFrame: sourceStart + index + 1,
      inputSourceBoundaryDeliveredAfterContextFrame: (
        sourceStart + index + 1
      ),
      inputSourceBoundaryReceivedContextFrameBefore: (
        sourceStart + index + 2
      ),
      inputSourceBoundaryReceivedContextFrameAfter: (
        sourceStart + index + 2
      ),
      inputSourceBoundaryReceivedClientMs: index + 999.5,
      inputChunkEmittedContextFrame: sourceStart + index + 3,
    })),
    {
      stage: 'input_ended',
      timestamp: 0.5,
      chunkIndex: -1,
      sourcePositionSec: 3 / 16000,
      audioBytes: 0,
    },
    {
      stage: 'audio_received',
      timestamp: 1,
      chunkIndex: 0,
      sourcePositionSec: 0,
      audioBytes: 2,
      audioMetadataProtocolVersion: 1 as const,
      streamGeneration: 1,
      parentSequenceId: 0,
      audioFrameId: 0,
    },
    {
      stage: 'playback_chunk_scheduled',
      timestamp: 2,
      chunkIndex: 0,
      sourcePositionSec: 0,
      audioBytes: 2,
      audioMetadataProtocolVersion: 1 as const,
      streamGeneration: 1,
      parentSequenceId: 0,
      audioFrameId: 0,
      scheduledStartContextSec: 106 / 16000,
      scheduledEndContextSec: 107 / 16000,
      scheduledStartContextFrameFloor: 106,
      scheduledEndContextFrameExclusive: 107,
    },
    {
      stage: 'audio_parent_complete',
      timestamp: 3,
      chunkIndex: -1,
      sourcePositionSec: 0,
      audioBytes: 2,
      streamGeneration: 1,
      parentSequenceId: 0,
      audioFrameCount: 1,
    },
    {
      stage: 'server_terminal',
      terminalStatus: 'completed' as const,
      timestamp: 4,
      chunkIndex: -1,
      sourcePositionSec: 3 / 16000,
      audioBytes: 0,
    },
  ];
  return {
    capture,
    input,
    inputHash,
    sourceSchedule,
    config,
    clientEvents,
    translatedTransportEvidence,
  };
}

describe('rendered-digital manifest', () => {
  it('binds only allowlisted runtime and digital claim evidence', async () => {
    const {
      capture,
      input,
      inputHash,
      sourceSchedule,
      config,
      clientEvents,
      translatedTransportEvidence,
    } = await fixture();
    const manifest = await buildRenderedDigitalManifest({
      capture,
      sourceSnapshot: {
        pcm: input.buffer,
        sampleRateHz: 16000,
        sampleCount: input.length,
        pcmSha256: inputHash,
        sourceFileSha256: 'f'.repeat(64),
      },
      sourceSchedule,
      config: {
        ...config,
        modelConfig: {
          ...config.modelConfig!,
          asr: {
            ...config.modelConfig!.asr,
            endpoint: 'private-host:50052',
          },
        } as AudioConfig['modelConfig'],
      },
      clientEvents,
      adaptivePlaybackEnabled: true,
      timingCsv: 'source,stage\n',
      blockLedgerCsv: 'schema,sequence\n',
      dashboardPhase: 'completed',
      translatedTransportEvidence,
    });

    expect(manifest).toMatchObject({
      schema: 'rendered-digital-common-clock-preflight/v1',
      evidence_status: 'unverified_browser_export',
      claims: {
        common_sample_clock_recorded: true,
        semantic_latency_status: 'not_evaluated',
        physical_dac_output_proven: false,
        acoustic_audibility_proven: false,
        audience_reaction_alignment_proven: false,
      },
      source_reference: {
        input_pcm_frame_count: 3,
        capture_frame_start: 2,
        capture_frame_end_exclusive: 5,
        outside_active_nonzero_sample_count: 0,
      },
      translated_output: {
        nonzero_outside_scheduled_sample_count: 0,
      },
      translated_transport: {
        received_frame_count: 1,
        scheduled_frame_count: 1,
        received_pcm_byte_count: 2,
        scheduled_pcm_byte_count: 2,
      },
      runtime: {
        asr: {
          profile_id: 'nemotron-asr-streaming_en-US_batch32',
        },
        nmt: {
          model_id: 'megatronnmt_any_any_1b',
          language_pair_id: 'en-US_to_es-US',
        },
        tts: {
          profile_id: 'magpie-tts-multilingual_batch8',
          voice_id: 'Magpie-Multilingual.ES-US.Isabela',
        },
        browser_capture: {
          frontend_repository_commit: '1'.repeat(40),
          frontend_repository_dirty: false,
          worklet_module_sha256: 'd'.repeat(64),
        },
      },
    });
    expect(JSON.stringify(manifest)).not.toContain('private-host');
  });

  it('rejects source audio outside the declared active interval', async () => {
    const {
      capture,
      input,
      sourceSchedule,
      config,
      clientEvents,
      translatedTransportEvidence,
    } = await fixture();
    capture.sourcePcm16[0] = 7;

    await expect(buildRenderedDigitalManifest({
      capture,
      sourceSnapshot: {
        pcm: input.buffer,
        sampleRateHz: 16000,
        sampleCount: input.length,
        pcmSha256: await sha256Hex(input.buffer),
        sourceFileSha256: 'f'.repeat(64),
      },
      sourceSchedule,
      config,
      clientEvents,
      adaptivePlaybackEnabled: true,
      timingCsv: 'source,stage\n',
      blockLedgerCsv: 'schema,sequence\n',
      dashboardPhase: 'completed',
      translatedTransportEvidence,
    })).rejects.toThrow(/outside its active interval/);
  });

  it('rejects translated PCM that changes before scheduling', async () => {
    const {
      capture,
      input,
      sourceSchedule,
      config,
      clientEvents,
      translatedTransportEvidence,
    } = await fixture();
    translatedTransportEvidence.scheduled[0].pcm = (
      Int16Array.of(901).buffer
    );

    await expect(buildRenderedDigitalManifest({
      capture,
      sourceSnapshot: {
        pcm: input.buffer,
        sampleRateHz: 16000,
        sampleCount: input.length,
        pcmSha256: await sha256Hex(input.buffer),
        sourceFileSha256: 'f'.repeat(64),
      },
      sourceSchedule,
      config,
      clientEvents,
      adaptivePlaybackEnabled: true,
      timingCsv: 'source,stage\n',
      blockLedgerCsv: 'schema,sequence\n',
      dashboardPhase: 'completed',
      translatedTransportEvidence,
    })).rejects.toThrow(/changed between receipt and playback scheduling/);
  });

  it('rejects translated samples outside every scheduled interval', async () => {
    const {
      capture,
      input,
      sourceSchedule,
      config,
      clientEvents,
      translatedTransportEvidence,
    } = await fixture();
    capture.translatedPcm16[0] = 1;

    await expect(buildRenderedDigitalManifest({
      capture,
      sourceSnapshot: {
        pcm: input.buffer,
        sampleRateHz: 16000,
        sampleCount: input.length,
        pcmSha256: await sha256Hex(input.buffer),
        sourceFileSha256: 'f'.repeat(64),
      },
      sourceSchedule,
      config,
      clientEvents,
      adaptivePlaybackEnabled: true,
      timingCsv: 'source,stage\n',
      blockLedgerCsv: 'schema,sequence\n',
      dashboardPhase: 'completed',
      translatedTransportEvidence,
    })).rejects.toThrow(/outside scheduled intervals/);
  });

  it('rejects a server terminal before parent completion', async () => {
    const {
      capture,
      input,
      sourceSchedule,
      config,
      clientEvents,
      translatedTransportEvidence,
    } = await fixture();
    const terminal = clientEvents.pop()!;
    clientEvents.splice(clientEvents.length - 1, 0, terminal);

    await expect(buildRenderedDigitalManifest({
      capture,
      sourceSnapshot: {
        pcm: input.buffer,
        sampleRateHz: 16000,
        sampleCount: input.length,
        pcmSha256: await sha256Hex(input.buffer),
        sourceFileSha256: 'f'.repeat(64),
      },
      sourceSchedule,
      config,
      clientEvents,
      adaptivePlaybackEnabled: true,
      timingCsv: 'source,stage\n',
      blockLedgerCsv: 'schema,sequence\n',
      dashboardPhase: 'completed',
      translatedTransportEvidence,
    })).rejects.toThrow(/observed after server completion/);
  });
});
