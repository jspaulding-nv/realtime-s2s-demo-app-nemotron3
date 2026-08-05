# Frame-boundary freshness-cap counterfactual — 2026-08-04

## Decision

A hard 10-second scheduled-playback queue is technically achievable on the
validated five-minute schema-3 trace if the client may discard individual
not-yet-audible 500 ms PCM frames. Do not deploy that raw policy: it retained
90.89% of translated audio but cut nine parents internally, including seven
with multiple fragmented gaps.

This was a deterministic offline counterfactual. It changed no live playback,
container, model request, PCM file, or browser behavior. The result contains no
transcript, translation, PCM, input path, endpoint, session identity, or wall
clock.

## Why this experiment was next

The prior complete-parent policy could not guarantee a 10-second queue bound.
One five-minute trace peaked at 14.059 seconds even after skipping eight of 74
parents because protected, active, or incomplete parents were not eligible for
atomic eviction. Several translated parents were themselves longer than the
desired queue.

The new policy asks the narrower capacity question: if the scheduler may evict
the oldest received frame that has not started and is outside a 100 ms
cancellation guard, can it hold the bound? It preserves the existing causal
1.00x/1.05x/1.10x rate decisions and compacts only future scheduled frames.

## Validated input

| Property | Value |
|---|---:|
| Duration | five minutes of real-time-paced source |
| Translated parents | 74 |
| Schema-3 translated frames | 2,782 |
| Frame duration | 500 ms except parent-final partial frames |
| Generated translated audio | 274.369 s |
| Primary cancellation guard | 100 ms |

The existing loader revalidated the schema-3 capture, ordered frame and parent
identities, byte totals, terminal state, and capture hashes before simulation.

## Results

| Cap | Hard cap achieved | Audio retained | Frames dropped | Fully dropped parents | Partially cut parents | Queue p95 | Peak queue | Tail |
|---:|:---:|---:|---:|---:|---:|---:|---:|---:|
| 5 s | yes | 73.74% | 723 | 0 | 25 | 4.880 s | 5.000 s | 8.686 s |
| 8 s | yes | 85.77% | 392 | 0 | 12 | 7.511 s | 8.000 s | 11.652 s |
| 10 s | yes | 90.89% | 250 | 0 | 9 | 9.142 s | 9.999 s | 13.674 s |

At 10 seconds, none of the nine affected parents was removed cleanly:

- two contained one contiguous internal gap;
- seven contained multiple fragmented gaps; and
- none was a simple prefix or suffix truncation.

The hard-cap result was stable at 0, 50, 100, and 250 ms cancellation guards.
Audio retention remained approximately 90.9%, with eight to ten partially cut
parents and no residual cap breach.

## Interpretation

This closes one architecture question. The 10-second target is not impossible
at the browser scheduler, but a hard guarantee conflicts with uninterrupted
speech on this workload. Fine-grained eviction achieves the timing number by
creating audible holes inside translated utterances.

The result also explains why whole-parent eviction and no-drop playback sit on
opposite sides of the tradeoff:

- whole-parent eviction preserves acoustic integrity but cannot always enforce
  the cap; and
- frame eviction enforces the cap but corrupts acoustic continuity.

Neither policy should be enabled live without an explicit product overload
decision and listening-quality approval.

## Next technical gate

Model a **single-tail truncation** policy before writing live browser code:

1. when a parent first causes an unavoidable breach, retain any already-started
   portion;
2. cancel the rest of that parent as one suffix, including future frames that
   arrive before its completion marker;
3. compact the remaining queue only at the parent boundary;
4. report whether 5/8/10-second caps are achieved, audio retained, parents
   truncated, maximum suffix removed, and residual breaches; and
5. treat a short fade/restart as a later listening-quality design, not as part
   of the capacity result.

This policy should eliminate scattered internal holes. It may sacrifice more
audio and still may not guarantee the cap while a long frame or protected
region is active; the offline result must decide that before any opt-in shadow
scheduler is added to the browser.

## Reproduction

```bash
PYTHONPATH=.:backend:.python-packages \
python3 analyze_frame_freshness_cap.py \
  --results-csv <schema3-results.csv> \
  --summary-json <schema3-summary.json>
```

Generated detailed JSON and Markdown stay under ignored
`experiment_results/` directories.
