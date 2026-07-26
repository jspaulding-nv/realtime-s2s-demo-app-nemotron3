# Staged ASR to NMT to TTS pipeline

## Status

The staged path is implemented and connected to `/ws/translate` behind
`S2S_PIPELINE_MODE=staged`. Monolithic mode remains the default and rollback
path. The first foundation milestone is documented in
[Staged pipeline foundation](STAGED_PIPELINE_FOUNDATION.md). It adds direct
Nemotron streaming ASR, typed final/segment records, deterministic punctuation
segmentation, and an opt-in live smoke. The second milestone provides
direct NMT/TTS adapters, bounded overlapping workers, ordered drain, and a
successful one-minute live preflight; see
[Bounded staged NMT and TTS pipeline](STAGED_NMT_TTS_PIPELINE.md) and
[feature-flagged WebSocket integration](STAGED_WEBSOCKET_INTEGRATION.md).
The later, fail-closed short-segment recovery is specified separately in
[Narrow NMT recovery for short punctuated segments](NMT_SHORT_SEGMENT_RECOVERY.md).

The first full staged sample canary is also complete. Sample 03 attempt 3
processed all 1,888.1045 seconds, delivered 646 consecutive ordered sequence
IDs, and reached natural completion with no pipeline, WebSocket, or cleanup
errors. This passes the operational gate, but not the audience-experience
gate: fixed 1.00x listener playback still ended 64.038 seconds after the
source. A later standalone Sample 02 canary from recovery commit `55b59bd`
also passed with 805 ordered segments and three validated NMT recoveries, but
its fixed listener tail was 239.156 seconds. The later clean staged matrix
completed all three samples with 2,027/2,027 ordered segments, but adaptive
queue p95 remained 23-61 seconds. Browser-independent scheduled-digital
validation, marked-phrase/punchline timing, and native-listener quality remain
open. Browser Web Audio is an optional renderer-specific cross-check, not a
deployment requirement.

The monolithic endpoint still connects to remote ASR and TTS services without
application-owned stage boundaries. Staged mode makes punctuation,
queue-residence, overlap, ordering, and content-safety decisions observable
and controllable while using the same pinned NMT and TTS models.

This work remains necessary after the offline adaptive replay. Although the
1.10x policy reduced aggregate simulated listener tail by 82.3%, queue p95 was
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
- Feed ordered PCM to an adaptive listener queue.
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
`SpeechSynthesisService.synthesize_online`. The repository pins the tested
client to `nvidia-riva-client==2.24.0`; direct ASR, NMT, and TTS smokes have
validated its request shapes and response streaming behavior against the three
pinned NIM releases.

## Implemented data flow

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
      +-- isolated hesitation filler --> telemetry-only discard before ID allocation
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
at terminal punctuation. Its contract:

- emit complete clauses or sentences on `.`, `?`, and `!` boundaries;
- retain any unpunctuated residual for the next final;
- preserve punctuation and whitespace needed by NMT/TTS;
- flush the residual exactly once on end-of-input;
- never emit empty segments;
- assign sequence IDs in source order; and
- apply a configurable maximum text length or age so a missing punctuation
  mark cannot buffer indefinitely.

Exact standalone hesitation fillers `uh`, `um`, `er`, `erm`, and `hmm` are
suppressed case-insensitively, including terminal punctuation and quote
wrappers. This happens before sequence-ID allocation, so subsequent meaningful
segments remain consecutive. Each suppression emits a privacy-safe
`segmenter/filler_discarded` record with contributing ASR-final IDs, source
timing, character count, and no translated-segment ID. It also increments the
session's `fillers_discarded` total. A filler embedded in meaningful text is
preserved.

The maximum-length/age fallback is a safety valve, not a substitute for the
800 ms ASR endpoint. Record why each segment was emitted: punctuation,
length, age, or final flush. Unit tests should cover punctuation across final
boundaries, multiple sentences in one final, abbreviations, decimals, empty
finals, Unicode punctuation, and end-of-input residuals.

## NMT-to-TTS content safety

The staged path applies narrow deterministic `es-US` source overrides only to
a small fixed allowlist of standalone expressions, preserving supported
punctuation and matched quote wrappers. These bypass the NMT RPC and remain
observable through `source_override_applied`. Sentence context and all other
short utterances still use NMT.

Every translated result is NFC-normalized and validated immediately after NMT,
then validated defensively again before TTS. The current `es-US` policy
requires speakable letter/digit content and permits Latin letters, decimal
digits, an explicit Spanish Magpie-safe punctuation allowlist, non-control
whitespace, and Latin-attached combining marks. CJK punctuation such as
`U+3002`, non-Latin or mixed-script letters, detached marks, symbols, and
control/format characters fail closed with sequence-scoped, privacy-safe
metadata. Invalid output is never sent to Magpie. The pipeline never retries
an unchanged NMT payload because that cannot make deterministic invalid text
safe and can produce inconsistent or wrong-language audio. After validation
has passed, the atomic TTS adapter may repeat the same request once only for a
server-side gRPC `UNKNOWN`; no failed-attempt PCM is published.

One diagnosed request-shape boundary has a narrower alternate input. If the
first result raises `TargetTextValidationError`, exact `es-US` source text
containing 1-32 ASCII letters plus one `.`, `?`, or `!` may be requested
exactly once with only that punctuation removed. The original segment,
sequence ID, source timing, ASR-final provenance, and emission reason remain
unchanged. The second response must pass cardinality, exact response-language,
and full target-text validation before TTS. RPC, cardinality, language,
ineligible-input, and second-attempt failures remain terminal.

## Bounded queues and backpressure

Use bounded `asyncio.Queue` instances between stages. Make capacities
configuration values and expose both item count and residence time. A sensible
initial experiment is a small number of sentence segments, then tune from
measured processing time rather than guessing a production limit.

The direct-ASR event queue defaults to 32 events per stream. Bounded NMT, TTS,
and atomic-audio output queues are also implemented with default data
capacities of four items each. The output path reserves one additional control
slot so an error/completion record cannot deadlock behind full audio data.

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
- Fail closed on invalid or wrong-script target text before TTS, with no
  unchanged NMT retry and no TTS call.
- Permit only the documented one-shot punctuation-normalized alternate request
  for the diagnosed deterministic request-shape boundary. This is not a
  transient retry, and a second failure remains terminal.
- Permit one unchanged TTS retry only for the separately diagnosed transient
  gRPC `UNKNOWN`; both attempts remain inside one overall deadline.
- Keep TTS retry output atomic so a partial first attempt is not followed by a
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
source_override_applied
```

The session summary includes `nmt_retry_count` and `tts_retry_count`, each
equal to the sum of `retry_count` over that stage's completed events. Each
event is constrained to zero or one. Exhausted recovery records its attempt
on the stage error without increasing the completed-recovery total.

Recommended events are:

```text
asr_final
segment_emitted
filler_discarded
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

`filler_discarded` is intentionally a pre-sequence event: it carries final
provenance and source timing but no segment ID. The session summary retains its
aggregate count. Source-override use is attached to the translated segment so
known short-form handling is auditable without logging source or target text.

Emit compact per-session summaries in addition to event-level CSV. Do not mix
backend epoch timestamps and browser `performance.now()` without an explicit
offset measurement.

## Implementation sequence

1. **Completed:** validate the pinned Riva Python client `2.24.0` against the
   direct ASR endpoint and add a repeatable live ASR smoke.
2. **Completed:** add typed ASR-stream/final, segment, and telemetry models,
   the punctuation segmenter, a bounded ASR event bridge, deterministic
   cancellation/lifecycle handling, and unit tests.
3. **Completed:** add application-owned direct NMT/TTS adapters and
   single-worker bounded queues with fake-client tests.
4. **Completed:** add ordered atomic audio and deterministic sentinel-based
   drain tests.
5. **Completed:** retain stage telemetry and expose session summaries/reports.
6. **Completed:** integrate the existing WebSocket API behind a staged feature
   flag while retaining `end_input` and `stop_stream` semantics.
7. **Completed:** pass the one-minute preflight and the first full staged
   sample operational canary. Sample 03 attempt 3 processed 1,888.1045 seconds
   and delivered all 646 ordered IDs without errors.
8. **Completed:** diagnose the pinned NMT short-token punctuation boundary and
   add one fail-closed punctuation-normalized recovery with retry telemetry.
9. **Completed:** run one full standalone Sample 02 recovery canary with
   805/805 ordered segments and three validated recoveries.
10. **Completed:** after the VM restart, relaunch FastAPI, pass a fresh
    preflight, and run a clean three-sample comparison matrix with identical
    models, input, EOU, and playback policy.
11. Run the browser-independent scheduled-digital gate and measure
    marked-phrase or punchline delay; optionally cross-check the selected
    renderer on a common clock. The matrix's 228.357-second fixed Sample 02
    tail and 38.158-second adaptive queue p95 leave this audience gate open.
12. Increase workers only if stage telemetry justifies it.

## Validation gates

- Every nonempty ASR final is represented in emitted-segment provenance, a
  pre-ID `filler_discarded` record, or the terminal residual.
- Every emitted segment ID reaches ordered output or has an explicit error
  record.
- Every successful NMT recovery retains its original identity/provenance,
  reports one retry, and passes target validation before TTS.
- End-of-input drains every stage and the listener schedule without a fixed
  arbitrary sleep.
- Queue bounds hold under injected slow-NMT and slow-TTS tests.
- The application exposes overload instead of dropping speech.
- Stage timing explains the difference between semantic delay and listener
  queue depth.
- The first full staged operational canary completes with consecutive output
  IDs and one natural terminal. Sample 03 attempt 3 passed this gate.
- Three sample runs complete without gRPC, WebSocket, or container failure.
- Audience queue and marked-joke delay improve without unacceptable Spanish
  quality.
