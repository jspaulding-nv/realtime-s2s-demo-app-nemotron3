import type { PlaybackScheduleEvent } from '../hooks/useAudioPlayback';
import type { LoadedFilePcmSnapshot } from '../hooks/useFileAudioSource';
import {
  RENDERED_DIGITAL_CAPTURE_SCHEMA,
  RENDERED_DIGITAL_CHANNEL_COUNT,
  RENDERED_DIGITAL_MAX_MAIN_THREAD_LAG_FRAMES,
  RENDERED_DIGITAL_PREFLIGHT_FILE_SHA256,
  RENDERED_DIGITAL_PREFLIGHT_PCM_SHA256,
  RENDERED_DIGITAL_SAMPLE_RATE_HZ,
  RENDERED_DIGITAL_SOURCE_CHUNK_FRAMES,
  RENDERED_DIGITAL_SOURCE_FRAMES,
  type RenderedDigitalCaptureResult,
} from '../types/renderedDigitalCapture';
import type { AudioConfig } from '../types/messages';
import type { ClientTimingEvent } from '../types/timing';
import { DEFAULT_PLAYBACK_POLICY } from './playbackPolicy';
import { sha256Hex } from './sha256';
import { FRONTEND_BUILD_PROVENANCE } from './frontendBuildProvenance';

const SHA256_PATTERN = /^(?:sha256:)?[0-9a-f]{64}$/;
const APPROVED_ASR_PROFILE_SELECTOR =
  'name=nemotron-asr-streaming,type=en-US,batch_size=32';
const APPROVED_NMT_MODEL = 'megatronnmt_any_any_1b';
const APPROVED_TTS_PROFILE_SELECTOR =
  'name=magpie-tts-multilingual,batch_size=8';
const APPROVED_TTS_VOICE = 'Magpie-Multilingual.ES-US.Isabela';
const APPROVED_SOURCE_LANGUAGE = 'en-US';
const APPROVED_TARGET_LANGUAGE = 'es-US';

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(',')}]`;
  }
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(record[key])}`
    )).join(',')}}`;
  }
  return JSON.stringify(value);
}

function utf8(value: string): ArrayBuffer {
  return new TextEncoder().encode(value).buffer as ArrayBuffer;
}

function concatBuffers(buffers: ArrayBuffer[]): ArrayBuffer {
  const byteCount = buffers.reduce(
    (total, buffer) => total + buffer.byteLength,
    0,
  );
  if (!Number.isSafeInteger(byteCount)) {
    throw new Error('Translated PCM evidence is too large to reconcile.');
  }
  const concatenated = new Uint8Array(byteCount);
  let offset = 0;
  for (const buffer of buffers) {
    concatenated.set(new Uint8Array(buffer), offset);
    offset += buffer.byteLength;
  }
  return concatenated.buffer;
}

function requireDigest(
  value: string | null | undefined,
  label: string,
): string {
  if (typeof value !== 'string' || !SHA256_PATTERN.test(value)) {
    throw new Error(`${label} is not bound to an immutable SHA-256 digest.`);
  }
  return value;
}

export interface RenderedDigitalManifestInputs {
  capture: RenderedDigitalCaptureResult;
  sourceSnapshot: LoadedFilePcmSnapshot;
  sourceSchedule: PlaybackScheduleEvent;
  config: AudioConfig;
  clientEvents: ClientTimingEvent[];
  adaptivePlaybackEnabled: boolean;
  timingCsv: string;
  blockLedgerCsv: string;
  dashboardPhase: 'completed';
  translatedTransportEvidence: TranslatedTransportEvidence;
}

export interface TranslatedTransportFrame {
  sequence: number;
  streamGeneration: number;
  parentSequenceId: number;
  audioFrameId: number;
  sampleRateHz: number;
  channels: number;
  bytesPerSample: number;
  pcm: ArrayBuffer;
}

export interface TranslatedTransportEvidence {
  received: TranslatedTransportFrame[];
  scheduled: TranslatedTransportFrame[];
}

interface FrameLedgerEntry {
  sequence: number;
  stream_generation: number;
  parent_sequence_id: number;
  audio_frame_id: number;
  sample_rate_hz: number;
  channels: number;
  bytes_per_sample: number;
  audio_bytes: number;
}

function frameLedgerEntry(
  frame: TranslatedTransportFrame,
  expectedSequence: number,
): FrameLedgerEntry {
  if (
    frame.sequence !== expectedSequence
    || !Number.isSafeInteger(frame.streamGeneration)
    || frame.streamGeneration < 0
    || !Number.isSafeInteger(frame.parentSequenceId)
    || frame.parentSequenceId < 0
    || !Number.isSafeInteger(frame.audioFrameId)
    || frame.audioFrameId < 0
    || frame.sampleRateHz !== RENDERED_DIGITAL_SAMPLE_RATE_HZ
    || frame.channels !== 1
    || frame.bytesPerSample !== 2
    || frame.pcm.byteLength === 0
    || frame.pcm.byteLength % (frame.channels * frame.bytesPerSample) !== 0
  ) {
    throw new Error('Translated PCM transport evidence is malformed.');
  }
  return {
    sequence: frame.sequence,
    stream_generation: frame.streamGeneration,
    parent_sequence_id: frame.parentSequenceId,
    audio_frame_id: frame.audioFrameId,
    sample_rate_hz: frame.sampleRateHz,
    channels: frame.channels,
    bytes_per_sample: frame.bytesPerSample,
    audio_bytes: frame.pcm.byteLength,
  };
}

function eventMatchesFrame(
  event: ClientTimingEvent,
  frame: TranslatedTransportFrame,
  sequence: number,
): boolean {
  return (
    event.chunkIndex === sequence
    && event.audioBytes === frame.pcm.byteLength
    && event.streamGeneration === frame.streamGeneration
    && event.parentSequenceId === frame.parentSequenceId
    && event.audioFrameId === frame.audioFrameId
    && event.audioMetadataProtocolVersion === 1
  );
}

export async function buildRenderedDigitalManifest({
  capture,
  sourceSnapshot,
  sourceSchedule,
  config,
  clientEvents,
  adaptivePlaybackEnabled,
  timingCsv,
  blockLedgerCsv,
  dashboardPhase,
  translatedTransportEvidence,
}: RenderedDigitalManifestInputs): Promise<Record<string, unknown>> {
  if (
    capture.channelCount !== RENDERED_DIGITAL_CHANNEL_COUNT
    || sourceSnapshot.sampleRateHz !== capture.sampleRateHz
    || sourceSnapshot.sampleCount !== RENDERED_DIGITAL_SOURCE_FRAMES
    || sourceSnapshot.sourceFileSha256
      !== RENDERED_DIGITAL_PREFLIGHT_FILE_SHA256
    || sourceSnapshot.pcmSha256 !== RENDERED_DIGITAL_PREFLIGHT_PCM_SHA256
    || sourceSchedule.playbackRate !== 1
    || sourceSchedule.audioBytes !== sourceSnapshot.sampleCount * 2
  ) {
    throw new Error('Source-reference capture metadata is inconsistent.');
  }

  const sourceStartContextFrame = (
    sourceSchedule.scheduledStartContextFrameFloor
  );
  if (
    !Number.isSafeInteger(sourceStartContextFrame)
    || sourceStartContextFrame < 0
    || !Number.isFinite(sourceSchedule.scheduledStartContextSeconds)
    || Math.abs(
      sourceSchedule.scheduledStartContextSeconds
      - sourceStartContextFrame / capture.sampleRateHz
    ) > 1 / capture.sampleRateHz
  ) {
    throw new Error('Source start is not an exact AudioContext frame.');
  }
  const sourceEndContextFrameExclusive = (
    sourceStartContextFrame + sourceSnapshot.sampleCount
  );
  if (
    sourceSchedule.scheduledStartContextFrameFloor
      !== sourceStartContextFrame
    || sourceSchedule.scheduledEndContextFrameExclusive
      !== sourceEndContextFrameExclusive
  ) {
    throw new Error(
      'Source-reference integer schedule does not match its active interval.',
    );
  }
  const sourceCaptureFrameStart = (
    sourceStartContextFrame - capture.captureStartContextFrame
  );
  const sourceCaptureFrameEndExclusive = (
    sourceCaptureFrameStart + sourceSnapshot.sampleCount
  );
  if (
    sourceCaptureFrameStart < 0
    || sourceCaptureFrameEndExclusive > capture.frameCount
  ) {
    throw new Error('Source-reference interval falls outside the capture.');
  }

  const activeSource = capture.sourcePcm16.slice(
    sourceCaptureFrameStart,
    sourceCaptureFrameEndExclusive,
  );
  const activeSourceSha256 = await sha256Hex(activeSource.buffer);
  if (activeSourceSha256 !== sourceSnapshot.pcmSha256) {
    throw new Error(
      'Rendered source reference does not reproduce the transmitted PCM.',
    );
  }
  let outsideActiveNonzeroSampleCount = 0;
  for (let frame = 0; frame < capture.sourcePcm16.length; frame += 1) {
    if (
      (
        frame < sourceCaptureFrameStart
        || frame >= sourceCaptureFrameEndExclusive
      )
      && capture.sourcePcm16[frame] !== 0
    ) {
      outsideActiveNonzeroSampleCount += 1;
    }
  }
  if (outsideActiveNonzeroSampleCount !== 0) {
    throw new Error(
      'Source-reference channel contains audio outside its active interval.',
    );
  }

  const playbackRows = clientEvents.filter(
    (event) => event.stage === 'playback_chunk_scheduled',
  );
  const receivedRows = clientEvents.filter(
    (event) => event.stage === 'audio_received',
  );
  const parentRows = clientEvents.filter(
    (event) => event.stage === 'audio_parent_complete',
  );
  const chunkRows = clientEvents.filter(
    (event) => event.stage === 'chunk_sent',
  );
  const inputEndedRows = clientEvents.filter(
    (event) => event.stage === 'input_ended',
  );
  const terminalRows = clientEvents.filter(
    (event) => event.stage === 'server_terminal',
  );
  if (
    dashboardPhase !== 'completed'
    || terminalRows.length !== 1
    || terminalRows[0].terminalStatus !== 'completed'
  ) {
    throw new Error(
      'Rendered-digital export requires one completed server terminal.',
    );
  }
  const terminalIndex = clientEvents.indexOf(terminalRows[0]);
  if (clientEvents.slice(terminalIndex + 1).some((event) => (
    event.stage === 'chunk_sent'
    || event.stage === 'audio_parent_complete'
    || event.stage === 'audio_received'
    || event.stage === 'playback_chunk_scheduled'
  ))) {
    throw new Error(
      'Pipeline content was observed after server completion.',
    );
  }
  const expectedChunkCount = (
    sourceSnapshot.sampleCount / RENDERED_DIGITAL_SOURCE_CHUNK_FRAMES
  );
  if (
    !Number.isSafeInteger(expectedChunkCount)
    || chunkRows.length !== expectedChunkCount
    || inputEndedRows.length !== 1
    || clientEvents.indexOf(inputEndedRows[0])
      <= clientEvents.indexOf(chunkRows.at(-1) as ClientTimingEvent)
    || clientEvents.indexOf(inputEndedRows[0]) >= terminalIndex
    || inputEndedRows[0].audioBytes !== 0
    || Math.abs(
      inputEndedRows[0].sourcePositionSec
      - sourceSnapshot.sampleCount / capture.sampleRateHz
    ) > Number.EPSILON
  ) {
    throw new Error(
      'Source completion and server terminal evidence did not reconcile.',
    );
  }
  for (let index = 0; index < chunkRows.length; index += 1) {
    const event = chunkRows[index];
    const expectedStart = index * RENDERED_DIGITAL_SOURCE_CHUNK_FRAMES;
    const expectedEnd = expectedStart + RENDERED_DIGITAL_SOURCE_CHUNK_FRAMES;
    if (
      event.chunkIndex !== index
      || event.audioBytes !== RENDERED_DIGITAL_SOURCE_CHUNK_FRAMES * 2
      || event.inputSourceSampleStart !== expectedStart
      || event.inputSourceSampleEndExclusive !== expectedEnd
      || event.inputSampleRateHz !== RENDERED_DIGITAL_SAMPLE_RATE_HZ
      || event.inputPcmSha256 !== sourceSnapshot.pcmSha256
      || event.inputPcmSampleCount !== sourceSnapshot.sampleCount
      || event.inputLedgerValid !== true
      || event.inputSourceBoundaryContextFrame
        !== sourceStartContextFrame + expectedEnd
      || event.inputSourceBoundaryDeliveredAfterContextFrame === undefined
      || event.inputSourceBoundaryDeliveredAfterContextFrame
        < event.inputSourceBoundaryContextFrame
      || event.inputSourceBoundaryDeliveredAfterContextFrame
        - event.inputSourceBoundaryContextFrame >= 128
      || event.inputSourceBoundaryReceivedContextFrameBefore === undefined
      || event.inputSourceBoundaryReceivedContextFrameAfter === undefined
      || event.inputChunkEmittedContextFrame === undefined
      || event.inputSourceBoundaryReceivedContextFrameBefore
        < event.inputSourceBoundaryDeliveredAfterContextFrame
      || event.inputSourceBoundaryReceivedContextFrameAfter
        < event.inputSourceBoundaryReceivedContextFrameBefore
      || event.inputChunkEmittedContextFrame
        < event.inputSourceBoundaryReceivedContextFrameAfter
      || event.inputSourceBoundaryReceivedContextFrameAfter
        - event.inputSourceBoundaryContextFrame
        > RENDERED_DIGITAL_MAX_MAIN_THREAD_LAG_FRAMES
      || event.inputChunkEmittedContextFrame
        - event.inputSourceBoundaryContextFrame
        > RENDERED_DIGITAL_MAX_MAIN_THREAD_LAG_FRAMES
      || event.inputSourceBoundaryReceivedClientMs === undefined
      || !Number.isFinite(event.inputSourceBoundaryReceivedClientMs)
      || event.inputChunkEmittedClientMs === undefined
      || !Number.isFinite(event.inputChunkEmittedClientMs)
      || event.inputChunkEmittedClientMs
        < event.inputSourceBoundaryReceivedClientMs
    ) {
      throw new Error('Source common-clock chunk ledger is inconsistent.');
    }
  }
  if (playbackRows.length === 0 || receivedRows.length === 0) {
    throw new Error('Translated output was not received and scheduled.');
  }
  if (
    translatedTransportEvidence.received.length !== receivedRows.length
    || translatedTransportEvidence.scheduled.length !== playbackRows.length
    || receivedRows.length !== playbackRows.length
  ) {
    throw new Error(
      'Translated PCM receipt and schedule evidence did not reconcile.',
    );
  }
  const receivedLedger = translatedTransportEvidence.received.map(
    (frame, index) => frameLedgerEntry(frame, index),
  );
  const scheduledLedger = translatedTransportEvidence.scheduled.map(
    (frame, index) => frameLedgerEntry(frame, index),
  );
  for (let index = 0; index < receivedRows.length; index += 1) {
    if (
      !eventMatchesFrame(
        receivedRows[index],
        translatedTransportEvidence.received[index],
        index,
      )
      || !eventMatchesFrame(
        playbackRows[index],
        translatedTransportEvidence.scheduled[index],
        index,
      )
    ) {
      throw new Error(
        'Translated PCM frame identity does not match the timing ledger.',
      );
    }
  }
  const streamGenerations = new Set(
    receivedRows.map((event) => event.streamGeneration),
  );
  if (
    streamGenerations.size !== 1
    || receivedRows[0].streamGeneration === undefined
    || receivedRows[0].streamGeneration <= 0
  ) {
    throw new Error(
      'Translated PCM spans an invalid number of stream generations.',
    );
  }
  const streamGeneration = receivedRows[0].streamGeneration;
  const parentIds: number[] = [];
  for (const event of receivedRows) {
    const parentId = event.parentSequenceId;
    if (parentId === undefined) {
      throw new Error('Translated PCM lacks a parent sequence identity.');
    }
    if (parentIds.at(-1) !== parentId) parentIds.push(parentId);
  }
  if (
    parentIds.some((parentId, index) => parentId !== index)
    || parentRows.length !== parentIds.length
  ) {
    throw new Error('Translated parent sequences are gapped or reordered.');
  }
  for (let parentId = 0; parentId < parentIds.length; parentId += 1) {
    const receivedForParent = receivedRows.filter(
      (event) => event.parentSequenceId === parentId,
    );
    const scheduledForParent = playbackRows.filter(
      (event) => event.parentSequenceId === parentId,
    );
    const complete = parentRows[parentId];
    const completionIndex = clientEvents.indexOf(complete);
    const lastReceivedIndex = clientEvents.indexOf(
      receivedForParent.at(-1) as ClientTimingEvent,
    );
    const lastScheduledIndex = clientEvents.indexOf(
      scheduledForParent.at(-1) as ClientTimingEvent,
    );
    const nextReceivedIndex = parentId + 1 < parentIds.length
      ? clientEvents.indexOf(
          receivedRows.find(
            (event) => event.parentSequenceId === parentId + 1,
          ) as ClientTimingEvent,
        )
      : Number.POSITIVE_INFINITY;
    const nextScheduledIndex = parentId + 1 < parentIds.length
      ? clientEvents.indexOf(
          playbackRows.find(
            (event) => event.parentSequenceId === parentId + 1,
          ) as ClientTimingEvent,
        )
      : Number.POSITIVE_INFINITY;
    if (
      scheduledForParent.length !== receivedForParent.length
      || complete.parentSequenceId !== parentId
      || complete.streamGeneration !== streamGeneration
      || complete.audioFrameCount !== receivedForParent.length
      || complete.audioBytes !== receivedForParent.reduce(
        (total, event) => total + event.audioBytes,
        0,
      )
      || completionIndex <= lastReceivedIndex
      || completionIndex <= lastScheduledIndex
      || completionIndex >= terminalIndex
      || completionIndex >= nextReceivedIndex
      || completionIndex >= nextScheduledIndex
    ) {
      throw new Error(
        'Translated parent completion does not reconcile with its frames.',
      );
    }
  }
  const receivedPcm = concatBuffers(
    translatedTransportEvidence.received.map((frame) => frame.pcm),
  );
  const scheduledPcm = concatBuffers(
    translatedTransportEvidence.scheduled.map((frame) => frame.pcm),
  );
  const receivedPcmSha256 = await sha256Hex(receivedPcm);
  const scheduledPcmSha256 = await sha256Hex(scheduledPcm);
  const receivedFrameLedgerSha256 = await sha256Hex(
    utf8(canonicalJson(receivedLedger)),
  );
  const scheduledFrameLedgerSha256 = await sha256Hex(
    utf8(canonicalJson(scheduledLedger)),
  );
  if (
    receivedPcm.byteLength !== scheduledPcm.byteLength
    || receivedPcmSha256 !== scheduledPcmSha256
    || receivedFrameLedgerSha256 !== scheduledFrameLedgerSha256
  ) {
    throw new Error(
      'Translated PCM changed between receipt and playback scheduling.',
    );
  }

  const scheduledIntervals = playbackRows.map((event, index) => {
    const startSeconds = event.scheduledStartContextSec;
    const endSeconds = event.scheduledEndContextSec;
    const start = event.scheduledStartContextFrameFloor;
    const end = event.scheduledEndContextFrameExclusive;
    if (
      startSeconds === undefined
      || endSeconds === undefined
      || start === undefined
      || end === undefined
      || !Number.isFinite(startSeconds)
      || !Number.isFinite(endSeconds)
      || startSeconds < 0
      || endSeconds <= startSeconds
      || event.chunkIndex !== index
      || !Number.isSafeInteger(start)
      || !Number.isSafeInteger(end)
      || Math.abs(
        startSeconds - start / capture.sampleRateHz
      ) > 1 / capture.sampleRateHz
      || Math.abs(
        endSeconds - end / capture.sampleRateHz
      ) > 1 / capture.sampleRateHz
    ) {
      throw new Error('Translated playback schedule is malformed.');
    }
    if (
      start < capture.captureStartContextFrame
      || end > capture.captureEndContextFrameExclusive
      || (
        index > 0
        && start < (
          playbackRows[index - 1]
            .scheduledStartContextFrameFloor as number
        )
      )
    ) {
      throw new Error(
        'Translated playback schedule falls outside the capture or reorders.',
      );
    }
    return { start, end };
  });
  const mergedIntervals: Array<{ start: number; end: number }> = [];
  for (const interval of scheduledIntervals) {
    const previous = mergedIntervals.at(-1);
    if (previous && interval.start <= previous.end) {
      previous.end = Math.max(previous.end, interval.end);
    } else {
      mergedIntervals.push({ ...interval });
    }
  }
  let intervalIndex = 0;
  let nonzeroOutsideScheduledSampleCount = 0;
  for (
    let captureFrame = 0;
    captureFrame < capture.translatedPcm16.length;
    captureFrame += 1
  ) {
    const absoluteFrame = capture.captureStartContextFrame + captureFrame;
    while (
      intervalIndex < mergedIntervals.length
      && absoluteFrame >= mergedIntervals[intervalIndex].end
    ) {
      intervalIndex += 1;
    }
    const insideScheduledInterval = (
      intervalIndex < mergedIntervals.length
      && absoluteFrame >= mergedIntervals[intervalIndex].start
    );
    if (
      capture.translatedPcm16[captureFrame] !== 0
      && !insideScheduledInterval
    ) {
      nonzeroOutsideScheduledSampleCount += 1;
    }
  }
  if (nonzeroOutsideScheduledSampleCount !== 0) {
    throw new Error(
      'Translated capture contains audio outside scheduled intervals.',
    );
  }
  const scheduledEndSeconds = playbackRows.at(-1)?.scheduledEndContextSec;
  const scheduledEndContextFrameExclusive = (
    playbackRows.at(-1)?.scheduledEndContextFrameExclusive
  );
  if (
    scheduledEndSeconds === undefined
    || scheduledEndContextFrameExclusive === undefined
  ) {
    throw new Error('Translated output lacks an AudioContext endpoint.');
  }

  const modelConfig = config.modelConfig;
  const stagedConfig = config.stagedConfig;
  const repository = config.repositoryProvenance;
  if (
    config.pipelineMode !== 'staged'
    || !config.audioMetadataProtocolVersions?.includes(1)
    || !modelConfig
    || !stagedConfig
    || modelConfig.asr.profile !== APPROVED_ASR_PROFILE_SELECTOR
    || modelConfig.nmt.model !== APPROVED_NMT_MODEL
    || modelConfig.tts.profile !== APPROVED_TTS_PROFILE_SELECTOR
    || modelConfig.tts.voice !== APPROVED_TTS_VOICE
    || modelConfig.asr.sourceLanguage !== APPROVED_SOURCE_LANGUAGE
    || modelConfig.nmt.sourceLanguage !== APPROVED_SOURCE_LANGUAGE
    || modelConfig.nmt.targetLanguage !== APPROVED_TARGET_LANGUAGE
    || modelConfig.tts.targetLanguage !== APPROVED_TARGET_LANGUAGE
    || stagedConfig.segmentMaxChars !== 240
    || stagedConfig.segmentMaxAgeMs !== 2000
    || stagedConfig.asrEventQueueMaxSize !== 32
    || stagedConfig.nmtQueueMaxSize !== 4
    || stagedConfig.ttsQueueMaxSize !== 4
    || stagedConfig.outputQueueMaxSize !== 4
    || stagedConfig.nmtRpcTimeoutSeconds !== 15
    || stagedConfig.ttsRpcTimeoutSeconds !== 60
    || stagedConfig.ttsMaxSegmentAudioSeconds !== 60
    || stagedConfig.ttsMaxRetries !== 1
    || stagedConfig.ttsResponseChunkTelemetryEnabled !== false
    || stagedConfig.ttsSubsegmentMaxChars !== 0
    || stagedConfig.ttsSubsegmentMinChars !== 12
    || stagedConfig.closeTimeoutSeconds !== 10
    || stagedConfig.ttsIncrementalAtomicFallbackMaxChars !== 4
  ) {
    throw new Error(
      'Backend configuration is not the approved staged translation path.',
    );
  }
  if (
    repository?.commit === null
    || repository?.commit === undefined
    || !/^[0-9a-f]{40}$/.test(repository.commit)
    || repository.dirty !== false
    || FRONTEND_BUILD_PROVENANCE.commit !== repository.commit
    || FRONTEND_BUILD_PROVENANCE.dirty !== false
  ) {
    throw new Error(
      'Frontend and backend are not clean processes from the same commit.',
    );
  }
  const runtime = {
    repository_commit: repository?.commit ?? null,
    repository_dirty: repository?.dirty ?? null,
    pipeline_mode: config.pipelineMode,
    audio_metadata_protocol_version: 1,
    telemetry_schema_version: stagedConfig.telemetrySchemaVersion ?? null,
    punctuation_segmentation: {
      max_chars: stagedConfig.segmentMaxChars,
      max_age_ms: stagedConfig.segmentMaxAgeMs,
    },
    staged_execution: {
      asr_event_queue_max_size: stagedConfig.asrEventQueueMaxSize,
      nmt_queue_max_size: stagedConfig.nmtQueueMaxSize,
      tts_queue_max_size: stagedConfig.ttsQueueMaxSize,
      output_queue_max_size: stagedConfig.outputQueueMaxSize,
      nmt_rpc_timeout_seconds: stagedConfig.nmtRpcTimeoutSeconds,
      tts_rpc_timeout_seconds: stagedConfig.ttsRpcTimeoutSeconds,
      tts_max_segment_audio_seconds: (
        stagedConfig.ttsMaxSegmentAudioSeconds
      ),
      tts_max_retries: stagedConfig.ttsMaxRetries,
      tts_response_chunk_telemetry_enabled: (
        stagedConfig.ttsResponseChunkTelemetryEnabled
      ),
      tts_subsegment_max_chars: stagedConfig.ttsSubsegmentMaxChars,
      tts_subsegment_min_chars: stagedConfig.ttsSubsegmentMinChars,
      incremental_atomic_fallback_max_chars: (
        stagedConfig.ttsIncrementalAtomicFallbackMaxChars
      ),
      close_timeout_seconds: stagedConfig.closeTimeoutSeconds,
    },
    browser_capture: {
      frontend_repository_commit: FRONTEND_BUILD_PROVENANCE.commit,
      frontend_repository_dirty: FRONTEND_BUILD_PROVENANCE.dirty,
      worklet_module_sha256: requireDigest(
        capture.workletModuleSha256,
        'Recorder worklet module',
      ),
    },
    asr: {
      image_digest: requireDigest(
        modelConfig.asr.imageDigest,
        'ASR image',
      ),
      profile_id: 'nemotron-asr-streaming_en-US_batch32',
      eou_ms: modelConfig.asr.eouMs,
      word_time_offsets: modelConfig.asr.wordTimeOffsets,
    },
    nmt: {
      image_digest: requireDigest(
        modelConfig.nmt.imageDigest,
        'NMT image',
      ),
      model_id: APPROVED_NMT_MODEL,
      language_pair_id: 'en-US_to_es-US',
    },
    tts: {
      image_digest: requireDigest(
        modelConfig.tts.imageDigest,
        'TTS image',
      ),
      profile_id: 'magpie-tts-multilingual_batch8',
      voice_id: APPROVED_TTS_VOICE,
      incremental_publish_enabled: (
        stagedConfig.ttsIncrementalPublishEnabled ?? false
      ),
      incremental_frame_ms: stagedConfig.ttsIncrementalFrameMs ?? null,
    },
  };
  const runtimeConfigSha256 = await sha256Hex(
    utf8(canonicalJson(runtime)),
  );
  const timingCsvSha256 = await sha256Hex(utf8(timingCsv));
  const blockLedgerCsvSha256 = await sha256Hex(utf8(blockLedgerCsv));

  return {
    schema: RENDERED_DIGITAL_CAPTURE_SCHEMA,
    capture_id: globalThis.crypto.randomUUID(),
    created_at_utc: new Date().toISOString(),
    evidence_status: 'unverified_browser_export',
    run_terminal: {
      server_status: 'completed',
      dashboard_phase: dashboardPhase,
    },
    claims: {
      common_sample_clock_recorded: true,
      source_pcm_binding_recorded: true,
      rendered_graph_output_observed: (
        capture.translatedNonzeroSampleCount > 0
      ),
      protocol_pcm_no_loss_verified: false,
      queue_bound_verified: false,
      semantic_latency_status: 'not_evaluated',
      physical_dac_output_proven: false,
      acoustic_audibility_proven: false,
      translation_quality_proven: false,
      audience_reaction_alignment_proven: false,
    },
    clock: {
      basis: 'single_audio_context_render_quantum',
      sample_rate_hz: capture.sampleRateHz,
      channel_count: capture.channelCount,
      capture_start_context_frame: capture.captureStartContextFrame,
      capture_end_context_frame_exclusive: (
        capture.captureEndContextFrameExclusive
      ),
      capture_frame_count: capture.frameCount,
      block_count: capture.blocks.length,
      context_state_violation_count: (
        capture.contextStateViolationCount
      ),
      visibility_violation_count: capture.visibilityViolationCount,
    },
    channels: [
      {
        index: 0,
        role: 'source_reference',
        tap: 'post_playback_rate_pre_monitor_mute',
      },
      {
        index: 1,
        role: 'translated_output',
        tap: 'post_queue_post_playback_rate_pre_monitor_mute',
      },
    ],
    source_reference: {
      input_file_sha256: sourceSnapshot.sourceFileSha256,
      input_pcm_sha256: sourceSnapshot.pcmSha256,
      input_pcm_frame_count: sourceSnapshot.sampleCount,
      source_start_context_frame: sourceStartContextFrame,
      source_end_context_frame_exclusive: (
        sourceEndContextFrameExclusive
      ),
      capture_frame_start: sourceCaptureFrameStart,
      capture_frame_end_exclusive: sourceCaptureFrameEndExclusive,
      active_slice_pcm_sha256: activeSourceSha256,
      outside_active_nonzero_sample_count: (
        outsideActiveNonzeroSampleCount
      ),
      chunk_frames: RENDERED_DIGITAL_SOURCE_CHUNK_FRAMES,
      chunk_count: expectedChunkCount,
    },
    translated_output: {
      received_frame_count: receivedRows.length,
      scheduled_frame_count: playbackRows.length,
      completed_parent_count: parentRows.length,
      nonzero_sample_count: capture.translatedNonzeroSampleCount,
      nonzero_outside_scheduled_sample_count: (
        nonzeroOutsideScheduledSampleCount
      ),
      last_scheduled_end_context_frame_exclusive: (
        scheduledEndContextFrameExclusive
      ),
    },
    translated_transport: {
      received_frame_count: receivedLedger.length,
      scheduled_frame_count: scheduledLedger.length,
      received_pcm_byte_count: receivedPcm.byteLength,
      scheduled_pcm_byte_count: scheduledPcm.byteLength,
      received_ordered_pcm_sha256: receivedPcmSha256,
      scheduled_ordered_pcm_sha256: scheduledPcmSha256,
      received_frame_ledger_sha256: receivedFrameLedgerSha256,
      scheduled_frame_ledger_sha256: scheduledFrameLedgerSha256,
    },
    artifacts: {
      wav_sha256: capture.wavSha256,
      wav_byte_count: capture.wavBytes.byteLength,
      interleaved_pcm_sha256: capture.interleavedPcmSha256,
      interleaved_pcm_sample_count: capture.interleavedPcm16.length,
      source_channel_pcm_sha256: capture.sourcePcmSha256,
      translated_channel_pcm_sha256: capture.translatedPcmSha256,
      timing_csv_sha256: timingCsvSha256,
      timing_csv_byte_count: utf8(timingCsv).byteLength,
      block_ledger_csv_sha256: blockLedgerCsvSha256,
      block_ledger_csv_byte_count: utf8(blockLedgerCsv).byteLength,
    },
    runtime: {
      ...runtime,
      normalized_config_sha256: runtimeConfigSha256,
    },
    playback_policy: {
      adaptive_playback_enabled: adaptivePlaybackEnabled,
      loss_policy: 'no_drop',
      target_queue_seconds: DEFAULT_PLAYBACK_POLICY.targetQueueSeconds,
      urgent_queue_seconds: DEFAULT_PLAYBACK_POLICY.urgentQueueSeconds,
      limit_queue_seconds: DEFAULT_PLAYBACK_POLICY.limitQueueSeconds,
      catch_up_release_seconds: (
        DEFAULT_PLAYBACK_POLICY.catchUpReleaseSeconds
      ),
      urgent_release_seconds: DEFAULT_PLAYBACK_POLICY.urgentReleaseSeconds,
      normal_rate: DEFAULT_PLAYBACK_POLICY.normalRate,
      catch_up_rate: DEFAULT_PLAYBACK_POLICY.catchUpRate,
      urgent_rate: DEFAULT_PLAYBACK_POLICY.urgentRate,
    },
    queue_gate: {
      metric: 'exact_piecewise_linear_audio_context_schedule',
      p95_objective_seconds: 5,
      peak_limit_seconds: 10,
      result: 'pending_offline_validation',
    },
  };
}
