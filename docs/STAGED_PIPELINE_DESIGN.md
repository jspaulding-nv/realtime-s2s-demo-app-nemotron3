# Planned staged ASR to NMT to TTS pipeline

## Status

The first foundation milestone is implemented and documented in
[Staged pipeline foundation](STAGED_PIPELINE_FOUNDATION.md). It adds direct
Nemotron streaming ASR, typed final/segment records, deterministic punctuation
segmentation, and an opt-in live smoke. The active browser path remains
monolithic, and no staged sample run has been performed.

The current backend calls the monolithic streaming S2S operation on the NMT
service. That endpoint connects to remote ASR and TTS services, but the
application cannot observe or control punctuation segmentation, queue
residence, or pipeline overlap between individual stages.

The staged design makes those boundaries explicit while keeping the pinned
models unchanged initially.

This work remains necessary after the offline adaptive replay. Although the
1.10x policy reduced aggregate simulated listener tail by 82.2%, queue p95 was
still 19-45 seconds and every trace exceeded the 10-second soft/SLA ceiling.
Listener-side catch-up alone did not create a bounded queue on the saved
arrival patterns.

## Goals

- Use Nemotron 3 streaming ASR for English input.
- Finalize speech with the measured 800 ms EOU starting point.
- Split final ASR text at punctuation boundaries before translation.
- Overlap NMT and TTS work through bounded queues.
- Preserve source order and every utterance.
- Expose per-stage queue and processing latency.
- Feed ordered PCM to the adaptive browser queue.
- Treat a 10-second listener queue as a soft/SLA ceiling, never as permission
  to drop speech.

## Pinned services and addressing

| Stage | Pinned image | From host | Inside Compose network |
|---|---|---|---|
| ASR | `nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0` | `localhost:50052` | `asr:50052` |
| NMT | `nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2` | `localhost:50051` | `nmt:50051` |
| TTS | `nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0` | `localhost:50053` | `tts:50053` |

Keep NMT and TTS models and voice unchanged for the first staged comparison so
the effect of orchestration is not confounded with a model change.

The installed Riva Python client exposes the relevant operation families as
`ASRService.streaming_response_generator`,
`NeuralMachineTranslationClient.translate`, and
`SpeechSynthesisService.synthesize_online`. Confirm exact signatures against
the live services before coding the staged path. The repository pins the
tested client to `nvidia-riva-client==2.24.0`; direct ASR, NMT, and TTS smoke
tests must still verify that its request shapes and response streaming behavior
match the three pinned NIM releases.

## Proposed data flow

```text
English PCM stream
      |
      v
Nemotron streaming ASR
      |
      +-- INTERIM / FINAL / COMPLETE / ERROR
      v
bounded ordered ASR event queue (implemented; interims are observability-only)
      |
      v
punctuation-aware segmenter
      |
      v
bounded NMT input queue --> NMT worker --> bounded TTS input queue
                                               |
                                               v
                                      ordered TTS worker
                                               |
                                               v
                                  bounded outbound-audio queue
                                               |
                                               v
                                      WebSocket / browser
                                               |
                                               v
                            adaptive 1.00x/1.05x/1.10x playback
```

One NMT worker and one TTS worker are the safest initial configuration. They
still provide pipeline parallelism: NMT can translate segment `n+1` while TTS
synthesizes segment `n`. More workers require an explicit reorder buffer keyed
by sequence ID before audio is sent.

## ASR and endpointing

Configure Nemotron streaming ASR with:

- 16 kHz mono LINEAR_PCM;
- language `en-US`;
- automatic punctuation enabled;
- interim results for UI/diagnostics; and
- final endpointing `stop_history=800` ms as the starting point.

Nemotron streaming is RNNT-based. Do not apply the CTC-only two-pass
`stop_history_eou` settings that were used with Parakeet/Conformer pipelines.

Lower EOU is not automatically lower total delay. Very short finalization can
fragment syntax, increase NMT/TTS work, and worsen queue growth. The Riva team
found 800 ms plus punctuation splitting preferable to 300 ms without
punctuation handling for these sample files.

Every final must receive a monotonically increasing ASR-final ID plus source
start/end timing when available. Interims may update a transcript UI but must
not enter NMT.

The implemented direct-ASR adapter runs blocking Riva I/O on one worker and
hands `INTERIM`, `FINAL`, `COMPLETE`, and `ERROR` records to the event loop
through one bounded FIFO. Its worker blocks on a full queue, preserves event
order, verifies natural input-sentinel consumption, and has tested cancellation
and channel/executor cleanup. The synchronous WAV iterator is an exclusive
smoke-test path, not the application integration API.

## Punctuation-aware segmentation

The segmenter accumulates final ASR text and emits complete translation units
at terminal punctuation. Its contract should:

- emit complete clauses or sentences on `.`, `?`, and `!` boundaries;
- retain any unpunctuated residual for the next final;
- preserve punctuation and whitespace needed by NMT/TTS;
- flush the residual exactly once on end-of-input;
- never emit empty segments;
- assign sequence IDs in source order; and
- apply a configurable maximum text length or age so a missing punctuation
  mark cannot buffer indefinitely.

The maximum-length/age fallback is a safety valve, not a substitute for the
800 ms ASR endpoint. Record why each segment was emitted: punctuation,
length, age, or final flush. Unit tests should cover punctuation across final
boundaries, multiple sentences in one final, abbreviations, decimals, empty
finals, Unicode punctuation, and end-of-input residuals.

## Bounded queues and backpressure

Use bounded `asyncio.Queue` instances between stages. Make capacities
configuration values and expose both item count and residence time. A sensible
initial experiment is a small number of sentence segments, then tune from
measured processing time rather than guessing a production limit.

The direct-ASR event queue is implemented and defaults to 32 events per stream.
The NMT, TTS, and outbound queues described below are not implemented yet.

When a queue is full, the producer must await capacity and emit an overload
metric. It must not allocate an unbounded list or discard speech. Because a
live speaker cannot actually be paused, application backpressure is a memory
guard and overload signal—not a guarantee of audience latency. Sustained
production above TTS/browser consumption still requires faster synthesis,
more acceptable playback acceleration, or a product-level degraded mode.

Track these values per stage:

- current and maximum queue size;
- oldest-item age;
- enqueue-to-start residence time;
- processing duration;
- completion-to-next-stage enqueue time; and
- blocked-put count and duration.

The browser's 5-10 second queue objective should remain a separate metric from
text queue item counts. A sentence count cannot be converted reliably to
audio seconds until TTS produces media.

## Ordering and concurrency

Initial worker topology:

```text
ASR consumer: 1
NMT workers:   1
TTS workers:   1
WebSocket sender: 1
```

This overlaps stages while preserving order without a reorder buffer. If
profiling proves one stage is the bottleneck, increase its worker count only
after adding:

- sequence IDs on every request and output;
- a bounded reorder map;
- a next-sequence cursor;
- timeouts and failure placeholders; and
- a policy for retries that cannot synthesize the same segment twice.

Do not send segment `n+1` audio before segment `n` merely because it finished
first. Reordered sample sentences would be a correctness failure even if the
queue metric improved.

## Session lifecycle and drain

Natural end-of-input must use a deterministic drain protocol:

1. Close the microphone/file input side of the ASR stream.
2. Consume all remaining ASR finals.
3. Flush the punctuation segmenter's residual text.
4. Put one sentinel after the last NMT item.
5. Drain NMT and forward its sentinel only after all translations finish.
6. Drain TTS and forward its sentinel only after all audio finishes.
7. Drain the outbound WebSocket queue.
8. Keep the browser session open until the Web Audio queue reaches zero.
9. Send/record a final session summary, then close.

Manual cancellation is different: stop accepting new work, cancel or drain
workers according to a documented policy, stop audio scheduling, and record
which sequence IDs did not complete.

## Failure handling

- Give every session and segment a stable ID in logs.
- Retry only failures known to be transient and cap retry counts.
- Make TTS retry output atomic so a partial first attempt is not followed by a
  duplicated full segment.
- Surface stage failure to the WebSocket client with the affected sequence ID.
- Do not restart a stream in a tight loop without delay and an error budget.
- On worker failure, unblock dependent queues and terminate the session
  cleanly rather than leaving consumers waiting forever.
- Record container restarts, gRPC status codes, deadlines, and queue state at
  failure.

## Observability schema

Use a monotonic session clock for all application stages. A minimum event
record should include:

```text
session_id
sequence_id
asr_final_id
contributing_final_ids
emission_reason
stage
event
monotonic_ms
source_start_ms
source_end_ms
queue_depth
text_chars
audio_bytes
audio_duration_ms
retry_count
error_code
```

Recommended events are:

```text
asr_final
segment_emitted
nmt_enqueued
nmt_started
nmt_completed
tts_enqueued
tts_started
tts_first_audio
tts_completed
outbound_enqueued
websocket_sent
browser_received
browser_scheduled
browser_audible_start
browser_completed
```

Emit compact per-session summaries in addition to event-level CSV. Do not mix
backend epoch timestamps and browser `performance.now()` without an explicit
offset measurement.

## Implementation sequence

1. **Completed:** validate the pinned Riva Python client `2.24.0` against the
   direct ASR endpoint and add a repeatable live ASR smoke.
2. **Completed:** add typed ASR-stream/final, segment, and telemetry models,
   the punctuation segmenter, a bounded ASR event bridge, deterministic
   cancellation/lifecycle handling, and unit tests.
3. Add application-owned direct NMT/TTS adapters and single-worker bounded
   queues with fake-client tests.
4. Add ordered outbound audio and deterministic sentinel-based drain tests.
5. Persist stage telemetry and session summaries.
6. Integrate the existing WebSocket API behind a staged feature flag while
   retaining `end_input` and
   `stop_stream` semantics.
7. Run the one-minute preflight, then one sample, before the full matrix.
8. Compare monolithic and staged paths with identical models, input, EOU, and
   browser playback policy.
9. Increase workers only if stage telemetry justifies it.

## Validation gates

- Every nonempty ASR final is represented in emitted-segment provenance or the
  terminal residual.
- Every emitted segment ID reaches ordered output or has an explicit error
  record.
- End-of-input drains every stage and the browser without a fixed arbitrary
  sleep.
- Queue bounds hold under injected slow-NMT and slow-TTS tests.
- The application exposes overload instead of dropping speech.
- Stage timing explains the difference between semantic delay and browser
  queue depth.
- Three sample runs complete without gRPC, WebSocket, or container failure.
- Audience queue and marked-joke delay improve without unacceptable Spanish
  quality.
