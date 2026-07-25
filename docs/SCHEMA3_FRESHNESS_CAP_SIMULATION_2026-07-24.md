# Schema-3 whole-parent freshness-cap simulation

## Status

The deterministic follow-on to the five-minute incremental-TTS canary is
complete. It narrows the most plausible first live policy to a 10-second,
oldest-first emergency eviction rule, but it also shows that whole-parent
eviction cannot guarantee a 10-second bound on the captured workload.

This is a lossy offline counterfactual. It did not change browser behavior or
drop audio in a live session.

## Evidence and validation

The analyzer used the schema-3 arm from the five-minute run:

```text
experiment_results/streaming-tts-canary-20260724T232158Z-29cdf4e/streaming
```

The raw directory is ignored. The public findings below contain no transcript,
translation, PCM, endpoint, absolute path, session identifier, source
filename, or organization name.

The loader failed closed unless all of these conditions held:

- staged telemetry schema 3 and incremental TTS publication were enabled;
- the pipeline closed with a complete outcome, no failure, no cleanup error,
  and no incomplete parent;
- all three parent-summary layers agreed;
- produced, dequeued, and WebSocket-sent frame keys and byte lists agreed;
- parent IDs, frame IDs, parent frame counts, and parent byte totals reconciled;
- client CSV PCM indexes were contiguous;
- all 2,782 CSV PCM byte counts matched all 2,782 WebSocket sends in order;
- precise summary receive timestamps matched the CSV's rounded timestamps;
- the summary and CSV input-end timestamps matched; and
- exactly one completed terminal followed input end, with no later PCM.

The validated capture contains 74 complete parents and 274.369 seconds of
translated PCM. Unaccelerated source-PCM duration per parent was:

| Metric | Duration |
|---|---:|
| Minimum | 0.511 s |
| p50 | 2.740 s |
| p95 | 11.331 s |
| Maximum | 14.257 s |

Before playback-rate adjustment, 15 parents contained more than 5 seconds of
PCM and 6 contained more than 10 seconds. These values describe media volume,
not scheduled queue depth.

## Policy semantics

Each arriving frame first receives the existing causal 1.00x, 1.05x, or 1.10x
playback decision. Eviction is considered only when the resulting scheduled
browser queue exceeds the selected cap.

A parent is eligible only after its final frame has arrived and only while
every frame in that parent is still inaudible. A parent that is incomplete,
playing, or partially played is retained in full. After an eviction, retained
future frames are compacted without changing playback-rate decisions already
made from earlier arrivals.

The primary scenarios use a 100 ms cancellation guard. A parent is protected
if any scheduled frame begins at or before the current decision time plus that
margin. This models the fact that a browser cannot safely cancel audio
scheduled to begin effectively “now.”

Two strategies were evaluated:

- `oldest_first`: remove the oldest eligible parent, compact, and repeat only
  until the cap is met or no eligible parent remains.
- `jump_to_latest_complete`: protect the newest eligible complete parent and
  remove all older eligible parents on a breach.

The cap covers only translated audio already received and scheduled in the
browser. It excludes upstream ASR finalization, NMT, TTS, and publication time.
It is therefore not a complete speaker-to-listener or joke/punchline delay.

## No-drop baseline

The same trace replayed through the existing adaptive no-drop controller
produced:

| Metric | Result |
|---|---:|
| Queue p50 | 4.136 s |
| Queue p95 | 17.077 s |
| Peak queue | 23.412 s |
| Time above 10 s | 44.286 s |
| Playback window above 10 s | 14.19% |
| Listener tail | 27.120 s |
| Audio retained | 100% |

## Loss/freshness results

| Strategy | Cap | Hard cap achieved | Audio retained | Parents retained | Parents skipped | Queue p95 | Peak queue | Time above cap | Listener tail |
|---|---:|:---:|---:|---:|---:|---:|---:|---:|---:|
| Oldest first | 5 s | No | 77.05% | 60/74 | 14 | 7.697 s | 14.059 s | 36.083 s | 12.672 s |
| Jump to latest | 5 s | No | 82.74% | 61/74 | 13 | 9.113 s | 17.036 s | 67.005 s | 13.854 s |
| Oldest first | 8 s | No | 83.87% | 64/74 | 10 | 7.726 s | 14.059 s | 12.856 s | 12.672 s |
| Jump to latest | 8 s | No | 87.29% | 66/74 | 8 | 9.116 s | 17.036 s | 24.421 s | 13.854 s |
| Oldest first | 10 s | No | 85.77% | 66/74 | 8 | 8.352 s | 14.059 s | 4.180 s | 12.672 s |
| Jump to latest | 10 s | No | 90.42% | 70/74 | 4 | 9.221 s | 17.036 s | 9.808 s | 13.854 s |

Every loss was a complete parent. No scenario dropped a partial parent or
failed frame/byte accounting.

## Interpretation

The 10-second oldest-first policy is the best first engineering candidate from
this trace:

- it kept queue p95 below 10 seconds;
- it cut peak queue from 23.412 to 14.059 seconds;
- it cut listener tail from 27.120 to 12.672 seconds; and
- it retained more speech than the 5- or 8-second oldest-first policies.

That improvement still required skipping 8 of 74 parents and 39.056 seconds of
translated speech, leaving only 85.77% of the generated audio. It also spent
4.180 seconds above its nominal cap and peaked 4.059 seconds beyond it.

The jump-to-latest strategy retained more speech, but it was worse on peak
queue, time above cap, and listener tail at every tested threshold. Protecting
the newest complete parent can preserve a parent that is itself too long to
meet the limit.

The 10-second oldest-first headline is stable at 0, 50, and 100 ms
cancellation guards. At 250 ms it still skipped 8 parents, retained 86.83% of
audio, had an 8.354-second queue p95, and spent 4.203 seconds above the cap.
The selected victims changed, but the freshness conclusion did not.

The most important result is negative: none of the complete-parent policies
guaranteed its selected bound on this trace. The proof is the observed
residual-breach record after every eligible eviction, not a direct comparison
between unaccelerated parent PCM duration and rate-adjusted queue depth. When
the retained remainder is incomplete, already audible, or deliberately
protected, a whole-parent rule cannot remove it without cutting speech.

## Reproduction

When the ignored five-minute evidence is present:

```bash
python3 analyze_freshness_cap.py \
  --results-csv \
    experiment_results/streaming-tts-canary-20260724T232158Z-29cdf4e/streaming/shared-prefix_results.csv \
  --summary-json \
    experiment_results/streaming-tts-canary-20260724T232158Z-29cdf4e/streaming/shared-prefix_summary.json
```

The default command uses a 100 ms guard and reports 0/50/100/250 ms
sensitivity. It writes a compact Markdown report and a detailed privacy-safe
JSON companion beside the capture. Input basenames are replaced with fixed
role labels; the JSON includes numeric eviction events and residual breaches.

## Decision and next gate

Do not enable lossy playback yet and do not run the unchanged policy across
another long-form matrix.

The next safe implementation step is observation-only protocol support:

1. add versioned, opt-in parent/frame metadata before each binary PCM frame;
2. add an explicit parent-completion marker;
3. forward source start/end offsets without transcript text;
4. validate header/binary pairing, byte totals, frame order, generation, and
   completion in the browser;
5. measure real source-boundary-to-audibility freshness and hypothetical
   oldest-first drops without changing playback; and
6. only then add a short-lookahead parent queue behind a separate opt-in flag.

The live acceptance experiment must include synchronized source markers, such
as a known phrase or punchline, because browser queue depth alone does not
answer whether translated listeners hear a joke before or after the room
reacts.

Before a hard 10-second promise is possible, the product must choose at least
one of these tradeoffs:

- allow a mid-parent truncation or fade when stale audio is already active;
- bound parent duration upstream, then revalidate quality and TTS call
  amplification; or
- describe 10 seconds as a target with explicit residual-breach telemetry
  rather than a guaranteed cap.
