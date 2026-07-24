# Incremental TTS publication: five-minute matched canary

## Status

The schema-3 publication path passed its five-minute operational promotion
gate. It did not pass the live-audience queue objective.

The formal run came from clean commit
`29cdf4ed46546bef4a662f65048b3b66422bfc8a`:

```text
experiment_results/streaming-tts-canary-20260724T232158Z-29cdf4e
```

The report contains no transcript, audio, endpoint, session identifier,
customer name, or source filename. Raw evidence remains under the ignored
experiment directory.

## Matched design and operational result

The runner created one exact five-minute, 16 kHz mono PCM prefix and reused its
1,000 input chunks in both arms. It verified the pinned Nemotron 3 ASR, Riva
NMT, and Magpie TTS image tags and immutable digests before starting each
backend.

Both arms:

- produced the same 74 ASR/NMT parent structure;
- completed all 74 parents in order;
- recorded zero NMT and TTS retries;
- had no incomplete work, connection loss, terminal failure, or cleanup error;
- passed produced/dequeued/server-sent/client-received PCM reconciliation; and
- left every pinned NIM healthy afterward with about 65 GB of GPU memory free.

The schema-3 arm emitted 2,782 ordered PCM frames. Its only atomic-fallback
parent was sequence 7, whose validated target length was two characters. That
parent completed as six normal frames totaling 16,348 bytes. No `UNKNOWN`
occurred, so this run again validates policy selection and sustained
completion rather than exercising the live retry.

## Direct incremental-publication result

The direct causal comparison remains within the schema-3 arm. It excludes the
one intentional atomic fallback and uses each of the other 73 parents' own
generated PCM:

| Metric | p50 | p95 | Max |
|---|---:|---:|---:|
| First TTS response to first WebSocket PCM | 35.5 ms | 46.7 ms | 93.1 ms |
| First-publication lead over full TTS response | 348.1 ms | 1.659 s | 2.288 s |

The fallback parent's first WebSocket PCM followed its private atomic RPC
completion by 11.2 ms.

Schema 3 therefore sustained the intended mechanical benefit over five
minutes: normal parents begin reaching the browser well before their complete
TTS response is available.

## Cross-arm comparison remains confounded

Magpie again produced materially different speech in the two matched arms:

| Measurement | Atomic | Schema 3 |
|---|---:|---:|
| Generated audio | 296.382 s | 274.369 s |
| Output/input duration ratio | 0.988x | 0.915x |
| First translated audio | 15.594 s | 15.196 s |
| Service tail lag | 3.528 s | 3.708 s |
| Adaptive queue p95 | 18.464 s | 17.077 s |
| Adaptive listener tail | 28.669 s | 27.120 s |

Total generated duration differed by 7.43%, and individual parent differences
were much larger. The comparator therefore marks relative queue and tail
changes as inconclusive. The apparent cross-arm reductions must not be
attributed to incremental publication.

## The audience queue misses its target

The schema-3 arm's own no-drop playback trace is sufficient to reject the
current audience policy, without comparing it with the atomic arm.

The adaptive controller is configured for:

```text
target = 5 s
urgent = 8 s
limit = 10 s
rates = 1.00x / 1.05x / 1.10x
```

Observed schema-3 behavior was:

| Audience metric | Result |
|---|---:|
| Time-weighted queue p50 | 4.136 s |
| Time-weighted queue p95 | 17.077 s |
| Peak queue | 23.412 s |
| Time above the 10 s limit | 44.286 s |
| Playback window above the 10 s limit | 14.19% |
| Listener tail after source input | 27.120 s |
| Source audio played above 1.00x | 55.20% |
| Source audio played at 1.10x | 30.69% |
| Longest continuous 1.10x interval | 40.144 s |

The controller scheduled every frame and dropped nothing. Its 1.05x/1.10x
speed-up removed about 4.23 seconds from the fixed-rate listener tail, but it
could not enforce the nominal 10-second limit.

Overall generated Spanish was shorter than the five-minute English prefix, yet
the queue still grew because translated audio arrived in delayed bursts. Total
duration ratio alone therefore does not predict the listener's real-time
experience.

## Where the remaining delay lives

For schema 3, source-segment boundary to first WebSocket PCM was:

| Percentile | Delay |
|---|---:|
| p50 | 1.833 s |
| p95 | 4.220 s |
| max | 5.548 s |

This is encouraging for the server path after a segment boundary exists.
Incremental publication removes roughly 0.35 seconds at p50 and up to about
1.66 seconds at p95 of TTS-response withholding.

It does not solve two other delays:

1. first translated audio still begins after about 15.2 seconds, before the
   first useful source boundary has traversed the whole path; and
2. the browser's no-drop playback queue can add much more than 10 seconds
   later in the session.

A live joke can therefore still be heard well after the room reacts. This run
does not contain a synchronized punchline marker, so it does not assign one
exact joke-delay value.

## Decision and next experiment

Do not spend the next long-form matrix on the unchanged no-drop audience
policy. Schema 3 is operationally ready for broader testing, but the
five-minute trace already establishes that the existing controller cannot
hold the requested 5-10 second freshness bound.

The next experiment should be offline and deterministic over this saved
schema-3 arrival trace:

1. add a hard freshness-cap simulator at 5, 8, and 10 seconds;
2. drop only whole not-yet-audible parent units, never arbitrary PCM samples,
   and report skipped parent/audio percentages plus each discontinuity;
3. compare oldest-first eviction with a jump-to-live policy that preserves the
   newest complete parent;
4. retain 1.05x/1.10x playback before eviction, so dropping is a last resort;
5. measure queue p95/max, listener tail, time above cap, eviction frequency,
   longest continuous accelerated interval, and retained-audio percentage;
6. reject any policy that hides loss or implies full translation fidelity;
   and
7. only after selecting an explicit fidelity-versus-freshness policy, implement
   it in the browser and run synchronized phrase/punchline tests, followed by
   all three long-form fixtures.

The live-audience requirement is now a product tradeoff as much as a model
latency problem: a hard 5-10 second bound requires either enough acceleration
to catch up or an explicit rule for skipping stale translated speech.
