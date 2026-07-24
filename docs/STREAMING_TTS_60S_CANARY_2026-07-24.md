# Incremental TTS publication: 60-second formal canary

## Scope

This note records the first clean schema-3 failure, the narrow reliability
fix it motivated, and the successful formal rerun. It contains no transcript,
audio, endpoint, session identifier, customer name, or source filename.

The tested path remained:

```text
English audio -> Nemotron 3 streaming ASR -> Riva NMT -> Magpie Spanish TTS
```

Both formal arms used the same 60-second, 16 kHz mono PCM prefix, 200 input
chunks, pinned container releases and immutable image digests. Post-NMT TTS
subsegmentation remained disabled. The atomic control used telemetry schema 1;
the experimental arm used schema 3 with 100 ms PCM frames.

## First clean schema-3 failure

The first clean implementation run came from commit `9505679`:

```text
experiment_results/streaming-tts-canary-20260724T224821Z-9505679
```

Its atomic arm completed. The streaming arm reached source position 39.3
seconds and then reproduced the previously diagnosed Magpie failure shape:

- parent sequence 7;
- three source characters and two validated target characters;
- two already acknowledged PCM frames, totaling 6,400 bytes; and
- a server-side gRPC `UNKNOWN`.

The fail-closed safety contract worked. The two committed frames were sent
once, no retry or replay occurred after commitment, one terminal error was
emitted, parent 7 remained incomplete, and every NIM stayed healthy. That
behavior prevented duplicated stochastic speech, but it showed that
unconditional incremental publication removed the safe atomic retry available
for this known tiny target.

## Reliability fix

Commit `57c7ffaba085727051d855a3a1e41d060540c8b5` added a schema-3-only,
configurable fallback:

```dotenv
STAGED_TTS_INCREMENTAL_ATOMIC_FALLBACK_MAX_CHARS=4
```

After existing normalization and validation, targets of four characters or
fewer keep all PCM private until the TTS RPC completes. A genuine `UNKNOWN`
may be retried once before publication. Only a complete successful attempt is
reframed and committed through the ordinary bounded schema-3 queue. Targets
longer than four characters retain true incremental publication.

The proven failure shape is two characters. Four is a conservative envelope
for similarly tiny outputs and adds an atomic hold only to very short speech.
Zero disables the fallback. Telemetry identifies fallback parents at TTS,
output-dequeue, and WebSocket-completion barriers. Audience metrics include
them, while direct incremental-benefit distributions exclude them.

The complete post-fix Python suite passed 605 tests with one optional
local-trace test skipped. Python compilation, shell syntax, whitespace checks,
and a real legacy-schema-3 analysis also passed.

## Successful formal rerun

The clean rerun is:

```text
experiment_results/streaming-tts-canary-20260724T231640Z-57c7ffa
```

Both arms completed all 16 parents with:

- identical ASR-final, segment, and NMT-parent structure;
- exact produced, dequeued, server-sent, and client-received PCM accounting;
- zero NMT or TTS retries;
- no incomplete parent, cleanup error, connection loss, or terminal failure;
  and
- all three pinned NIMs healthy afterward with zero GPU utilization and about
  65 GB of GPU memory free.

The schema-3 arm produced 448 ordered frames. Parent 7 had the same validated
two-character target shape, was correctly classified as the only atomic
fallback parent, and completed as seven frames totaling 19,320 bytes. No
`UNKNOWN` happened in this rerun, so the formal run proves correct live policy
selection and end-to-end completion, not a live retry event. The earlier
exact-target diagnostic remains the direct live evidence that atomic retry can
recover this intermittent failure.

## Direct publication result

The causal measurement uses only the 15 true-incremental parents in the
schema-3 arm and compares each parent's first WebSocket frame with that same
parent's TTS completion:

| Metric | p50 | p95 |
|---|---:|---:|
| First TTS response to first WebSocket PCM | 35.5 ms | 48.6 ms |
| First-publication lead over full TTS response | 347.5 ms | 1.054 s |

The one fallback parent was excluded from those distributions. Its first
WebSocket PCM followed the private atomic RPC completion by 9.6 ms.

This establishes the narrow benefit: for normal parents, the browser can
receive PCM hundreds of milliseconds before it could receive an atomically
joined response. It does not establish microphone-to-ear latency or a bounded
listener queue.

## Cross-arm measurements are confounded

Magpie produced materially different speech in the two otherwise matched
arms:

| Measurement | Atomic | Schema 3 |
|---|---:|---:|
| Generated audio | 52.199 s | 43.886 s |
| Output/input duration ratio | 0.870x | 0.731x |
| First translated audio | 15.534 s | 15.134 s |
| Service tail lag | 1.111 s | 1.275 s |
| Adaptive queue p95 | 10.595 s | 7.915 s |
| Captured listener tail | 7.881 s | 4.802 s |

The generated-duration difference was 15.93%, and the largest per-parent byte
difference was much larger. Queue and tail improvements therefore combine
publication timing with a substantially lighter generated-audio workload.
The comparator correctly classifies every cross-arm queue/tail conclusion as
inconclusive. Those values must not be presented as a 39% audience-latency
improvement.

## Audience interpretation and next gate

This is an operational and direct-publication pass, not yet a live-audience
experience pass.

The first translated audio still arrived after about 15 seconds. Incremental
TTS publication cannot remove delay already accumulated in ASR endpointing,
segmentation, NMT, and the first TTS response. Over longer speech, generated
Spanish can also outlast incoming English and rebuild the browser playback
queue. A listener can therefore still hear a joke or other audience-reaction
moment noticeably after the room reacts.

The next promotion gate is a clean matched five-minute canary using the same
four-character fallback. If it passes, run the selected schema-3 policy over
all three long-form fixtures. The audience gate remains separate:

1. enforce or simulate a bounded listener queue of approximately 5-10 seconds;
2. measure synchronized source phrase/punchline to audible browser output,
   rather than inferring it from service tail;
3. retain no-drop 1.05x-1.10x playback as a quality candidate, with
   native-language review; and
4. treat any queue/tail comparison as causal only when generated workloads are
   comparable or when the measurement is within one generated stream.
