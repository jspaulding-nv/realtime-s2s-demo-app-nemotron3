# Bounded staged NMT and TTS pipeline

## Status

The second staged-pipeline milestone is implemented on
`agent/staged-nmt-tts-pipeline`. It joins direct Nemotron ASR, punctuation
segmentation, direct Riva NMT, and direct Magpie TTS through bounded FIFO
queues. A real-time one-minute English-to-Spanish preflight completed on July
22, 2026. The first full-length operational canary also passed: all 1,888.1045
seconds of *Long-form sample 03* completed through the staged WebSocket
path with 646 contiguous translated-audio sequence IDs and no pipeline,
cleanup, WebSocket, container, or integrity error. The later post-recovery
Sample 02 canary also passed with 805 contiguous IDs and three validated NMT
recoveries. Both remain standalone canaries rather than a completed,
provenance-frozen matrix.

This document records the direct orchestrator milestone and its standalone
smoke. The later feature-flagged browser integration is documented in
[Feature-flagged staged WebSocket integration](STAGED_WEBSOCKET_INTEGRATION.md).
The default remains monolithic; `S2S_PIPELINE_MODE=staged` now selects the
direct path through the existing `/ws/translate` protocol.

## What is implemented

| Component | Contract |
|---|---|
| `backend/direct_nmt_client.py` | One source segment per RPC, explicit deadline, exact-one/nonempty output, narrow known-short-utterance overrides, Spanish target-text validation, and one guarded punctuation-normalized recovery |
| `backend/direct_tts_client.py` | Spanish voice lookup, defensive pre-TTS target-text validation, 16 kHz mono Int16 PCM, full-segment atomic buffering, first-audio/completion timing, active-call cancellation, no retries |
| `backend/staged_pipeline.py` | One ASR consumer, one NMT worker, one TTS worker, bounded queues, ordered drain, first-failure ownership, per-stage telemetry |
| `staged_pipeline_smoke.py` | Real-time WAV feed, direct three-service run, raw PCM output, JSON report, and an overall terminal deadline |

NMT and TTS each use one worker. This preserves source order without a reorder
buffer while still overlapping stages: NMT can translate segment `n + 1`
while TTS synthesizes segment `n`.

TTS output is atomic per segment. Although Magpie streams response chunks, the
adapter publishes only after the RPC has completed and every nonempty chunk
has been validated. A failed request therefore cannot leak partial audio and
then duplicate it during a later attempt. TTS performs zero retries. NMT has
only the narrow, source-normalizing recovery described below; it never repeats
an unchanged request.

## Pinned services

| Stage | Image | Host gRPC endpoint |
|---|---|---|
| ASR | `nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0` | `localhost:50052` |
| NMT | `nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2` | `localhost:50051` |
| TTS | `nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0` | `localhost:50053` |

The tested client is `nvidia-riva-client==2.24.0`. NMT uses
`megatronnmt_any_any_1b`, and Spanish TTS uses
`Magpie-Multilingual.ES-US.Isabela`.

An important live finding is encoded as an adapter guard: the pinned NMT can
return fluent-looking output for an empty or whitespace-only request. Blank
segments are rejected before the RPC, and NMT output must contain exactly one
nonempty `es-US` translation before it can reach TTS.

The full-sample canary exposed a second content boundary. Configured isolated
hesitation fillers are suppressed by the segmenter before a sequence ID is
allocated, and each discard is recorded in privacy-safe telemetry. A small,
deterministic allowlist handles known standalone expressions whose
pinned-model outputs used the wrong script.

All other NMT output is normalized and validated immediately after NMT and
again before TTS: Spanish must contain at least one letter or digit, every
letter must be Latin script, and control/format characters, symbols, and
detached marks are rejected. Invalid output fails closed.

A later controlled replay isolated another pinned NMT `1.5.2` boundary: a
single short ASCII token followed by terminal punctuation produced invalid
wrong-script text in 5/5 isolated requests, while removing only the terminal
punctuation passed validation in 5/5 requests. Neighboring context also passed
in 5/5 requests for each tested context shape. The adapter therefore permits
exactly one punctuation-removed request only after
`TargetTextValidationError`, only for exact `es-US`, and only for 1-32 ASCII
letters followed by one `.`, `?`, or `!`. It retains the original segment,
sequence ID, and provenance, revalidates the second output, and still sends
nothing to TTS unless validation succeeds. RPC, cardinality, language,
ineligible-input, and second-attempt failures are not retried. See
[Narrow NMT recovery for short punctuated segments](NMT_SHORT_SEGMENT_RECOVERY.md).
A recovered completion records `retry_count=1` on its NMT event; the normal
path records zero, and the session summary's `nmt_retry_count` must equal the
sum across completed NMT events.

## Data flow and bounds

```text
English PCM
    -> Nemotron streaming ASR
    -> bounded ASR event bridge (32 events)
    -> punctuation/age/length segmenter
       -> suppress configured standalone hesitation fillers before ID allocation
    -> bounded NMT queue (4 segments)
    -> one NMT worker
    -> bounded TTS queue (4 translations)
    -> one TTS worker
    -> bounded output queue (4 atomic audio segments)
    -> smoke consumer or feature-flagged ordered WebSocket relay
```

All puts await capacity. Once a sequence ID is allocated, no translated text
or audio is discarded: every sequence must complete or make the session fail.
The only intentional content suppression is the configured standalone
hesitation-filler policy before ID allocation, and each suppression is counted
in telemetry. The queue bounds are memory and overload controls, not proof
that a live speaker can be paused or that audience delay will remain below a
fixed number of seconds.

The output queue reserves one additional control slot so `COMPLETE` or `ERROR`
cannot deadlock behind its configured audio-segment capacity. Each TTS response
chunk is capped at 256 KiB and one synthesized segment is capped at 60 seconds
of configured PCM by default. The queue still counts atomic segments rather
than playback seconds; browser/Web Audio instrumentation measures playback
backlog separately.

Natural completion is an exact ordered drain:

1. Stop accepting source audio and finish ASR input.
2. Consume remaining ASR finals.
3. Flush the segmenter's residual once.
4. Place a typed internal drain marker after the final NMT segment.
5. NMT processes every earlier item, then forwards the marker to TTS.
6. TTS processes every earlier translation, then publishes one `COMPLETE`.
7. The consumer drains all preceding audio before observing `COMPLETE`.

The first model-stage error owns the session failure, cancels sibling tasks,
and produces one terminal `ERROR` instead of `COMPLETE`. Direct NMT has a real
unary RPC deadline. Riva client 2.24 does not expose a deadline on the public
streaming-TTS helper, so the orchestrator enforces its deadline by cancelling
the retained call and closing the TTS channel.

## Configuration

Defaults are in `.env.example`:

```dotenv
S2S_PIPELINE_MODE=monolithic
STAGED_SEGMENT_MAX_CHARS=240
STAGED_SEGMENT_MAX_AGE_MS=2000
STAGED_ASR_EVENT_QUEUE_MAXSIZE=32
STAGED_NMT_QUEUE_MAXSIZE=4
STAGED_TTS_QUEUE_MAXSIZE=4
STAGED_OUTPUT_QUEUE_MAXSIZE=4
STAGED_NMT_RPC_TIMEOUT_SECONDS=15
STAGED_TTS_RPC_TIMEOUT_SECONDS=60
STAGED_TTS_MAX_SEGMENT_AUDIO_SECONDS=60
STAGED_CLOSE_TIMEOUT_SECONDS=10
```

`S2S_PIPELINE_MODE` now controls the browser backend path. It defaults to
`monolithic`; `staged_pipeline_smoke.py` still constructs the staged path
explicitly regardless of that value.

## Reproduce the live preflight

Docker Compose reads `.env` itself, but shell commands such as `docker login`
need the variables exported into the shell:

```bash
set -a
source .env
set +a

printf '%s' "$NGC_API_KEY" | \
  docker login nvcr.io --username '$oauthtoken' --password-stdin

docker compose up -d
docker compose ps
```

Once the three containers are healthy, the smoke itself does not need the NGC
key; it talks to the local gRPC ports:

```bash
PYTHONPATH=.python-packages python3 staged_pipeline_smoke.py \
  --file "${S2S_TEST_AUDIO_DIR:-test_audio}/preflight.wav" \
  --duration-seconds 60 \
  --pcm-output test_results_staged/staged-smoke-es-US.pcm \
  --json-output test_results_staged/staged-smoke-report.json
```

Generated PCM/JSON artifacts under `test_results_staged/` are ignored. Listen
to the raw output without adding a WAV header:

```bash
ffplay -f s16le -ar 16000 -ac 1 \
  test_results_staged/staged-smoke-es-US.pcm
```

Use `--fast` only for throughput testing. Audience-latency observations need
real-time source pacing.

## July 22, 2026 one-minute standalone result

The pinned containers were healthy on the 96 GB RTX PRO 6000 Blackwell Server
Edition. The preflight used 60 seconds from the local `preflight.wav`, real-
time pacing, English input, Spanish output, automatic punctuation, and 800 ms
EOU.

| Metric | Observed |
|---|---:|
| Input sent | 60.000 s |
| Wall time through terminal drain | 61.024 s |
| First translated audio available | 5.109 s after run start |
| Tail drain after source input ended | 1.307 s |
| Atomic audio segments | 23 |
| Segment reasons | 13 punctuation, 10 age |
| Output PCM | 1,612,414 bytes / 50.388 s |
| Output/input whole-prefix duration ratio | 0.840 |
| Maximum NMT / TTS / output queue depth | 2 / 2 / 2 |
| Blocked queue puts | 0 |
| NMT processing | 126.99–596.25 ms; 319.84 ms average |
| TTS first response audio | 120.85–170.82 ms; 145.98 ms average |
| TTS full-segment completion | 247.08–1,092.73 ms; 493.69 ms average |
| NMT queue residence | 13.17 ms average; 169.26 ms maximum |
| TTS queue residence | 5.22 ms average; 116.26 ms maximum |

This sample stayed caught up: it finished only 1.307 seconds after the input
window, no bounded queue saturated, and NMT/TTS work overlapped without
reordering. It establishes that the direct APIs and drain protocol work on the
deployed profiles. The later full-sample WebSocket canary established the
first sample-length operational pass described below.

The 0.840 duration ratio includes silence in the whole source WAV prefix. It is
not an aligned utterance-only TTS expansion measurement and should not be
compared directly with the Riva team's 6–10% Spanish expansion finding.

## Full Sample 03 staged WebSocket canary

The first full-length canary completed all 1,888.1045 seconds of *Long-form sample 03* at real-time input pace. It emitted, synthesized, dequeued, and
successfully WebSocket-sent the same contiguous 646 sequence IDs, `0` through
`645`. Six standalone fillers were intentionally suppressed before ID
allocation. The staged integrity validator passed with one ordered natural
completion, no PCM after completion, and no failure, cleanup error, incomplete
ID, container restart, or GPU OOM.

This is preserved historical evidence. It predates the narrow NMT recovery,
closed-export settling, and allowlisted failed-capture retention. The final
recovery snapshot was subsequently exercised by the standalone Sample 02
canary, but the current code still requires a fresh post-reboot preflight and
clean sample matrix.

| Metric | Observed |
|---|---:|
| Source input | 1,888.1045 s |
| First translated audio at client | 5.112942 s |
| Output/source duration ratio | 1.016270617x |
| Last-audio arrival after input ended | 0.849578 s |
| Completed-terminal arrival after input | 1.743608 s |
| Harness drain observation (poll/settle included) | 2.254558 s |
| Fixed-rate arrival-replay playback tail | 64.037605 s |
| Translated-audio sequences | 646, IDs 0–645 |
| Standalone fillers suppressed before ID allocation | 6 |
| Maximum NMT / TTS / output queue depth | 4 / 4 / 1 |
| Blocked NMT / TTS / output puts | 15 / 0 / 0 |
| Integrity, cleanup, WebSocket, container errors | none |

The bounded queues behaved correctly, including NMT backpressure rather than
drops when its four-item queue filled. Operational completion is separate from
audience latency: the fixed-rate replay accumulated a 64.037605-second tail,
so the long canary confirms the risk that Spanish playback can fall behind
even while last-audio and completed-terminal arrival tails stay short.

See [Sample 03 staged full-sample canary](LONG_FORM_03_STAGED_CANARY.md) for the
failure investigations, corrective policies, and promotion record. Raw event
streams, logs, plots, and runtime manifests are intentionally excluded from
the public repository.

## Post-recovery Sample 02 canary

Commit `55b59bd` subsequently completed a standalone real-time Sample 02
canary through the hardened staged path. The run reached `closed` / `complete`
with 805 emitted, produced, and completed segments; contiguous IDs 0–804; and
no incomplete ID, failure, cleanup error, connection loss, or timeout. The
narrow NMT recovery completed three times and preserved its original sequence
and provenance each time.

The queue peaks were NMT 4, TTS 4, and output 1. Backpressure blocked 17 NMT
puts and 5 TTS puts without dropping work. Last translated audio arrived only
0.308 seconds after input ended, but the translated PCM was 1.08831x the source
duration and fixed 1.00x playback ended 239.156 seconds late. The result
therefore closes the targeted recovery gate, not the audience-latency gate.

See
[Sample 02 post-recovery staged canary](STAGED_SAMPLE_02_RECOVERY_CANARY.md)
for the frozen image digests, full aggregate measurements, restart observation,
and exact next-run boundary.

## Audience-delay interpretation

First output arrived in roughly 5.1 seconds in both the short sample and full
canary, but that is not the same as punchline delay. A listener hears a
particular joke only after:

```text
source phrase timing
+ ASR finalization / segmentation
+ NMT and TTS processing
+ already queued Spanish playback
```

The staged report measures server-side components and an arrival-based
fixed-rate replay, but not executed browser Web Audio playback or alignment of
an English punchline with its Spanish rendition. The 64.038-second Sample 03
replay tail demonstrates why the audience objective remains a bounded playback
queue of roughly 5–10 seconds, with 10 seconds treated as a soft ceiling rather
than a guaranteed cap. A growing Spanish playback queue could make laughter
feel late as a sample continues even when the server's end-of-input arrival
tail is under one second.

## Remaining work

1. Relaunch FastAPI after the VM restart, run a fresh staged WebSocket
   preflight, and then run all three samples from one clean provenance-frozen
   commit. The standalone Sample 02 and historical Sample 03 canaries are
   evidence, not resumable matrix checkpoints.
2. Use the browser's existing 1.00x/1.05x/1.10x controller to record actual
   Web Audio queue seconds.
3. Add synchronized marked-phrase/joke measurements; service tail alone does
   not answer the audience-experience question.
4. If the browser queue still grows, evaluate 1.05–1.10x playback/prosody with
   native Spanish listeners and document quality tradeoffs.

The direct ASR input iterator itself is still unbounded. Bounds begin at the
ASR event bridge. For live microphone use, sustained upstream overload needs
an explicit degraded-mode policy rather than pretending application
backpressure can pause a speaker.

Direct adapters supplied to a session are exclusive for that session while it
is active. `owns_clients=False` suppresses automatic connect and normal idle
disconnect, but an RPC timeout or unfinished-session cancellation must still
abort the model call/channel; reconnect those supplied clients before reuse.
