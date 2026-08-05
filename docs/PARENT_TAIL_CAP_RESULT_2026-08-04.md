# Parent-tail freshness-cap counterfactual — 2026-08-04

## Decision

A single-tail truncation policy enforced 5-, 8-, and 10-second scheduled-audio
queue caps on the retained five-minute schema-3 trace. At the preferred
10-second cap, it retained 90.49% of translated audio and affected nine of 74
translated parents. Every affected parent kept one continuous prefix and lost
only its remaining suffix: there were zero prefix cuts, internal gaps, or
fragmented gaps.

This is a better overload shape than raw frame eviction, but it is still an
intentionally lossy offline counterfactual. It changed no live audio and does
not establish Spanish quality, semantic completeness, or an end-to-end
speaker-to-listener latency guarantee.

## Policy modeled

When scheduled translated audio exceeds the cap, the simulator:

1. protects audio that has started or is inside the 100 ms cancellation guard;
2. chooses the oldest eligible translated parent;
3. preserves the largest possible not-yet-audible prefix while removing enough
   of that parent's latest scheduled suffix to restore the cap;
4. suppresses any later frames from the same parent through its completion
   marker; and
5. compacts later scheduled audio without reordering it.

The suppression rule is what makes each affected parent have at most one
keep-to-drop transition. No fade, boundary repair, or live browser cancellation
is modeled.

## Validated input

| Property | Value |
|---|---:|
| Real-time-paced source duration | 300.067 s |
| Translated parents | 74 |
| Schema-3 translated frames | 2,782 |
| Generated translated audio | 274.369 s |
| Primary cancellation guard | 100 ms |

The existing loader revalidated the schema-3 capture, ordered frame and parent
identities, byte totals, terminal state, and capture hashes before simulation.
The generated detailed JSON and Markdown are private, ignored artifacts and do
not contain PCM, transcript text, translations, private paths, endpoints,
session identifiers, or wall-clock timestamps.

## Results

| Cap | Hard cap achieved | Audio retained | Frames dropped | Parents truncated | Queue p95 | Peak queue | Listener tail |
|---:|:---:|---:|---:|---:|---:|---:|---:|
| 5 s | yes | 72.62% | 763 | 25 | 4.649 s | 5.000 s | 8.629 s |
| 8 s | yes | 85.28% | 409 | 12 | 7.441 s | 8.000 s | 11.684 s |
| 10 s | yes | 90.49% | 265 | 9 | 9.133 s | 9.999 s | 13.649 s |

At 10 seconds, the longest removed suffix was 7.289 seconds. The
single-tail invariant held for all affected parents. The 10-second result was
also stable at 0, 50, 100, and 250 ms cancellation guards: each scenario held
the cap, retained about 90.49% of audio, and produced only suffix truncations.

## Comparison at the 10-second target

| Policy | Hard cap achieved | Audio retained | Damage shape | Queue p95 | Peak queue |
|---|:---:|---:|---|---:|---:|
| No drop | no | 100% | none | 17.077 s | 23.412 s |
| Whole-parent eviction | no | 85.77% | 8 complete parents skipped | 8.352 s | 14.059 s |
| Raw frame eviction | yes | 90.89% | 2 internal and 7 fragmented gaps | 9.142 s | 9.999 s |
| Single-tail truncation | yes | 90.49% | 9 clean trailing suffixes | 9.133 s | 9.999 s |

Single-tail truncation spends only 0.40 percentage points more audio than raw
frame eviction while eliminating scattered holes. It retains 4.72 percentage
points more audio than whole-parent eviction and, unlike that policy, enforces
the selected queue bound.

## Interpretation for real-time S2S

The result establishes a technically plausible emergency overload policy for
the browser's already-scheduled translated audio. A 10-second cap prevents
that queue from growing without bound, which is important for audience
freshness during long-form speech.

It does not mean every translated event is heard within ten seconds of the
English speaker. ASR endpointing, NMT, TTS generation, network delivery, and
the cancellation guard occur before or around this scheduled-audio queue. A
speaker-to-listener event-latency claim still requires synchronized source and
translated-event evidence.

The cost is also substantial: in this trace, approximately 9.5% of generated
Spanish audio would be omitted during overload, including one 7.289-second
tail. Without bilingual review, that could remove important meaning. The
policy must therefore remain disabled for audible playback.

## Next technical gate

Implement the same policy as an **observation-only live shadow scheduler**:

1. replay its decisions from browser parent/frame arrival metadata without
   cancelling or altering actual playback;
2. record projected queue depth, truncation triggers, retained duration, and
   suffix length per parent in privacy-safe telemetry;
3. verify the single-tail invariant and 10-second cap on new live five-minute
   runs, then across all three long-form samples; and
4. only after those objective gates pass, design an opt-in audible canary with
   a short fade and boundary-safe restart for later bilingual review.

This separates proof that the policy works causally under live timing from the
future judgment of whether its losses are acceptable to listeners.

## Reproduction

```bash
PYTHONPATH=.:backend:.python-packages \
python3 analyze_frame_freshness_cap.py \
  --strategy truncate_parent_tail \
  --results-csv <schema3-results.csv> \
  --summary-json <schema3-summary.json>
```

Detailed outputs are written beside the supplied trace under the ignored
`experiment_results/` tree.
