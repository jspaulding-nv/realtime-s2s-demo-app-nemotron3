# Staged pipeline foundation

## Status

The first staged-pipeline milestone is implemented on
`agent/staged-s2s-pipeline` without changing the active browser translation
path. The existing `/ws/translate` endpoint still uses the monolithic Riva S2S
operation, so the July 22 acceptance baseline remains directly comparable and
rollback remains trivial.

Implemented and validated:

- a shared Nemotron RNNT streaming-ASR configuration;
- a direct ASR adapter for `localhost:50052`;
- a bounded, ordered ASR-to-asyncio event bridge with real backpressure;
- typed interim, final, segment, and stage-event records;
- deterministic punctuation-aware segmentation of finalized ASR text;
- independent ASR-final and emitted-segment identities;
- maximum-length and residual-age safety valves;
- a standalone live direct-ASR smoke command; and
- focused and full-suite regression tests.

Not implemented in this milestone:

- direct NMT and TTS adapters in the application;
- bounded NMT/TTS queues and pipeline overlap;
- the staged WebSocket session and sentinel drain;
- target-language validation before TTS;
- per-stage event persistence and session summaries; or
- a staged sample-length latency comparison.

## Files and responsibilities

| File | Responsibility |
|---|---|
| `backend/asr_config.py` | One shared 16 kHz, automatic-punctuation, 800 ms EOU request for monolithic and direct ASR |
| `backend/direct_asr_client.py` | Blocking Riva ASR isolation, bounded asyncio event handoff, strict completion, and deterministic cancellation/channel cleanup |
| `backend/staged_models.py` | ASR stream events, ASR-final/segment identities, emission reasons, provenance, and future stage-event contracts |
| `backend/punctuation_segmenter.py` | Pure single-owner segmentation state machine |
| `direct_asr_smoke.py` | Opt-in live WAV compatibility and segmentation smoke |
| `direct_asr_bridge_smoke.py` | Opt-in live bounded-event-bridge and lifecycle smoke |
| `backend/tests/test_direct_asr_client.py` | Config, parser, FIFO ordering, saturated-queue backpressure, cancellation, failure, and lifecycle tests |
| `backend/tests/test_punctuation_segmenter.py` | Punctuation, provenance, fallback, lifecycle, and validation tests |
| `tests/test_direct_asr_smoke.py` | Complete-versus-partial WAV input-consumption tests |

The old monolithic `RivaS2SClient` now calls the same ASR config builder. This
prevents the direct and monolithic paths from silently diverging on EOU,
punctuation, encoding, sample rate, or language.

## ASR configuration

The foundation uses:

```text
gRPC endpoint:             localhost:50052
encoding:                  LINEAR_PCM
sample rate / channels:    16 kHz / mono
source language:           en-US
automatic punctuation:    enabled
interim results:           enabled for observability only
final EOU stop_history:    800 ms
word time offsets:         disabled by default
```

Nemotron is RNNT-based. The shared builder deliberately does not send the
CTC-only `stop_history_eou` or `stop_threshold_eou` fields.

Interims never enter the segmenter. A direct stream ending before the input
iterator consumes its stop sentinel is an error rather than false completion.
The synchronous WAV smoke separately verifies that every requested input frame
was consumed before it reports success.

The background adapter exposes `open_stream(event_queue_maxsize=32)`. Its
worker sends ordered `INTERIM` and `FINAL` events followed by exactly one
terminal `COMPLETE` or `ERROR` event after a natural input finish. A full queue
blocks the ASR worker instead of allocating unbounded output or dropping
events. Consumers use `await stream.next_event()`, source producers use
`add_chunk()` and `finish_input()`, and shutdown uses `await client.aclose()`.
Manual cancellation is intentionally different: it emits no terminal event;
after queued events drain, `next_event()` raises `DirectASRStreamClosed`.
Cancellation unblocks both the audio iterator and a worker waiting on a full
event queue. Opening a second stream or racing a new stream with client shutdown
fails explicitly.

The event-count bound protects ordering and memory at the ASR handoff. It is
not the audience's 5-10 second playback objective and does not yet bound NMT,
TTS, outbound audio, or end-to-end semantic delay.

## Segmenter contract

`PunctuationSegmenter` accepts only `AsrFinal` records. ASR final IDs and
translated segment IDs are intentionally different because their relationship
is many-to-many: one final can contain several sentences, and one sentence can
span several finals.

Normal terminal boundaries are `.`, `?`, `!`, `。`, `？`, `！`, and `…`.
Punctuation runs and closing quotes/brackets stay attached to their sentence.
The deterministic protections cover decimals, embedded alphanumeric dots,
common honorifics/abbreviations, and initialisms such as `U.S.`.
An ASCII period surrounded by alphanumeric characters remains protected, so
ambiguous text such as `Sentence.Next` is kept together rather than risking a
split inside a domain or version. `!` and `?` still terminate when ASR omits the
following space.

The initial experimental safety values are:

```text
STAGED_SEGMENT_MAX_CHARS=240
STAGED_SEGMENT_MAX_AGE_MS=2000
```

These are measurement starting points, not production guarantees. The age is
measured from the oldest buffered non-whitespace final. Punctuation wins if it
arrives exactly at the age deadline. Otherwise the emission reasons are
`punctuation`, `length`, `age`, or `final_flush`.

Every emitted segment records:

- a consecutive zero-based sequence ID;
- the exact emission reason;
- buffer and emission monotonic time;
- the contributing ASR-final IDs; and
- the coarse source timing envelope when ASR supplies one.

If any contributing final lacks a source start or end, that aggregate endpoint
remains unknown instead of fabricating a complete timing range from another
final.

`flush()` emits the residual at most once. Repeated flush calls return no
segments, and pushing more text after flush fails explicitly.

## Live smoke

With the pinned ASR container healthy, run the default 20-second, real-time
prefix of the one-minute test file:

```bash
export PYTHONPATH="$PWD/.python-packages:$PWD/backend:$PWD"

python direct_asr_smoke.py \
  --quiet \
  --json-output /tmp/s2s-eval-direct-asr-smoke.json
```

Use `--duration-seconds 0` for the full WAV or `--fast` for an RPC-shape check
without real-time pacing. The input must be uncompressed 16 kHz mono 16-bit
PCM WAV.

Observed on July 22, 2026 with Nemotron ASR Streaming `1.2.0` and Riva client
`2.24.0`:

```text
Audio sent:       20.0 seconds in 19.91 wall-clock seconds
Input completed:  true
Interim results:  59
Final results:    5
Segments:         4
Reasons:          3 punctuation, 1 age, 0 length, 0 final flush
```

The smoke confirmed that direct finals are fragments rather than ready-made
translation units. Several finals can contribute to one segment, validating
the need for punctuation accumulation and independent provenance IDs.

Run the checked-in bounded bridge smoke independently of the synchronous
compatibility command:

```bash
python direct_asr_bridge_smoke.py \
  --quiet \
  --json-output /tmp/s2s-eval-direct-asr-bridge-smoke.json
```

`--event-timeout-seconds` is terminal grace, not a per-event timer. The command
must reach `COMPLETE` or `ERROR` within the expected real-time send duration
plus that grace (30 seconds by default); recurring interims cannot extend the
deadline. Producer stop is allowed up to one second; the subsequent
`client.aclose()` operation has a separate 12-second outer bound.

The observed 20-second real-time run with
`open_stream(event_queue_maxsize=4)` produced 59 `INTERIM`, 5 `FINAL`, exactly
1 `COMPLETE`, and 0 `ERROR` events. That validates the background FIFO,
sentinel, segmentation, and async client-close path.

Separate operator probes also confirmed that the pinned direct NMT call and
streaming TTS call accept Riva client `2.24.0`. Those probes are compatibility
evidence only; application adapters and lifecycle tests are still required in
the next milestone.

## Validation

Focused foundation tests:

```bash
PYTHONPATH=.python-packages:backend \
PYTHONDONTWRITEBYTECODE=1 \
python -m pytest -p no:cacheprovider \
  backend/tests/test_punctuation_segmenter.py \
  backend/tests/test_direct_asr_client.py \
  tests/test_direct_asr_smoke.py \
  tests/test_direct_asr_bridge_smoke.py -q
```

Result: **79 passed**.

Full Python regression suite:

```bash
PYTHONPATH=.python-packages:backend:. \
PYTHONDONTWRITEBYTECODE=1 \
python -m pytest -p no:cacheprovider backend/tests tests -q
```

Result: **149 passed**.

Adapter tests use fake Riva services, and the smoke CLI tests are local. They
therefore cannot prove NIM compatibility; both checked-in opt-in live commands
are separate required gates.

## Next milestone

This historical next-milestone list is now implemented and validated in
[Bounded staged NMT and TTS pipeline](STAGED_NMT_TTS_PIPELINE.md). The next
remaining integration is the default-off staged WebSocket route.

Implement the staged text/audio worker path behind an explicit feature flag:

1. Add direct NMT and streaming TTS adapters with target-language/non-empty
   validation between them.
2. Add single-worker bounded NMT/TTS/output `asyncio.Queue` stages and preserve
   sequence order without a reorder buffer.
3. Add deterministic ASR flush -> NMT sentinel -> TTS sentinel -> outbound
   drain tests with injected slow/failing clients.
4. Persist `PipelineEvent` records and per-session summaries for queue
   residence, inference duration, blocked puts, retries, and failures.
5. Add a staged WebSocket mode while retaining the monolithic path as the
   control.
6. Pass the one-minute preflight and short excerpts before running a complete
   sample.
