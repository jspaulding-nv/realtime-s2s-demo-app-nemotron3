# Post-NMT TTS subsegment capacity model

## Decision

The completed three-sample matrix contains enough transcript-free telemetry to
size an initial post-NMT TTS subsegment experiment. Across 2,027 paired TTS
calls, translated character count explains most of the synthesized PCM
duration:

```text
audio seconds = 0.488769 + 0.056789 * translated characters
R² = 0.853396
```

Leave-one-sample-out residual validation gives a 44-character limit for a
4-second p95 envelope and a 46-character limit for an 8-second
observed-maximum-residual envelope. Requiring the aggregate, all three
per-sample fits, and the cross-sample envelope to pass within the observed
character range, then rounding down to a five-character configuration grid,
selects **40 translated characters** as the strict experimental cap.

This result does not justify enabling a 40-character production cap. The fitted
intercept also warns that every additional TTS call may add fixed pause or
utterance-boundary duration. Smaller chunks can reduce individual delivery
bursts while increasing total generated audio and therefore long-run listener
backlog. The smallest safe live experiment must measure both effects.

## Evidence boundary

The analyzer joins two existing privacy-safe events by sequence ID:

- `tts.started.text_chars` supplies the translated character count; and
- `tts.completed.audio_duration_ms` supplies the synthesized PCM duration.

The translated character count must also match `nmt.completed.text_chars` for
the same sequence.

It rejects a capture unless staged integrity passed, the pipeline closed with a
complete outcome, failure and cleanup fields are empty, sequence IDs are
contiguous, every start has one completion, parent counts reconcile, WebSocket
parent IDs reconcile, and TTS retry totals reconcile.

The generated analysis contains no transcript, audio, input path, filename,
endpoint, or session identifier. Samples are labeled only `sample_01` through
`sample_03`. The canonical structural-record digest for this matrix is:

```text
d0a925237bee08d815f203221eeae38c6e594cac80615b2a74c20cc343ace8af
```

The ignored source directory remains the provenance-frozen recovery matrix
documented in
[Staged recovery three-sample matrix](STAGED_RECOVERY_MATRIX_2026-07-24.md).
The 22-segment preflight is intentionally excluded.

## Model results

| Scope | Calls | Character p95 / max | Audio p95 / max | Seconds/character | R² | P95 cap | Max-envelope cap |
|---|---:|---:|---:|---:|---:|---:|---:|
| Aggregate | 2,027 | 120 / 277 | 7.709 / 17.090 s | 0.056789 | 0.853 | 45 | 45 |
| Sample 01 | 580 | 153 / 254 | 8.824 / 16.393 s | 0.055396 | 0.842 | 42 | 88 |
| Sample 02 | 804 | 117 / 242 | 7.616 / 16.765 s | 0.055630 | 0.828 | 45 | 49 |
| Sample 03 | 643 | 110 / 277 | 7.198 / 17.090 s | 0.060621 | 0.904 | 48 | 43 |

The leave-one-sample-out residual p95 is 0.957820 seconds and the largest
holdout residual is 4.897105 seconds. Applying those residuals to the aggregate
center line produces:

```text
p95 cap = floor((4.0 - 0.488769 - 0.957820) / 0.056789) = 44
max cap = floor((8.0 - 0.488769 - 4.897105) / 0.056789) = 46
```

At the strict 40-character grid point, the fitted aggregate/per-sample p95
envelopes range from 3.488 to 3.871 seconds. Observed-maximum-residual envelopes
range from 5.293 to 7.785 seconds. Of the original chunks, 1,076 were already
40 characters or shorter; their observed audio p95 was 2.879 seconds and their
maximum was 6.269 seconds.

Character count is not a hard duration bound. One short original chunk still
produced 6.269 seconds of audio. The 8-second value is an empirical sizing
envelope, not a guarantee about future text or newly split requests.

## Burst-size versus total-duration tradeoff

The following call counts are lower bounds calculated as the sum of
`ceil(original_characters / cap)` for every parent translation. Punctuation and
word-boundary constraints can require more calls. The extra-audio estimate
multiplies additional calls by the 0.488769-second aggregate OLS intercept; it
is a risk counterfactual, not a measured result of actual splitting.

| Cap | Minimum calls | Increase from 2,027 | Cross-sample p95 / max envelope | Intercept-based extra audio |
|---:|---:|---:|---:|---:|
| 40 | 3,482 | 71.8% | 3.718 / 7.657 s | 10.7% of captured output |
| 45 | 3,208 | 58.3% | 4.002 / 7.941 s | 8.7% of captured output |
| 60 | 2,717 | 34.0% | 4.854 / 8.793 s | 5.1% of captured output |

Forty characters is the strict sizing result. Forty-five characters is a
near-boundary candidate: its cross-sample p95 misses 4 seconds by about 2 ms
while its max envelope remains below 8 seconds. Sixty characters is an
efficiency candidate that reduces call amplification, but it misses both fitted
targets.

The correct choice cannot be made offline. If the intercept-based expansion
appears in live split synthesis, a strict 40-character cap could improve chunk
granularity and simultaneously make accumulated audience delay worse.

## Smallest safe implementation contract

Keep one complete NMT translation per source parent. Split only the translated
target text so NMT retains the current context window. Every TTS unit must have
a stable composite identity:

```text
(parent_sequence_id, subsequence_id, subsequence_count)
```

The first feature-flagged implementation should satisfy all of these
requirements:

1. Prefer the last sentence-ending punctuation before the cap, then softer
   punctuation, then whitespace, and finally a hard character boundary.
2. Keep punctuation and closing marks with the preceding text. Do not emit
   empty or punctuation-only units.
3. Merge tiny adjacent clauses within the same parent when the cap permits.
   Do not hold one parent open waiting for a future NMT parent.
4. Prove that the ordered subsegments preserve the translated text without
   loss or duplication under the splitter's documented whitespace
   normalization.
5. Process children in lexical parent/subsequence order. The next parent cannot
   overtake the current parent's final child.
6. Apply the existing private-buffer atomic TTS retry independently to each
   child. PCM from a failed attempt must never escape.
7. Emit one output frame per successful child. Queue the single terminal event
   only after every child frame.
8. Preserve parent source ranges and contributing ASR-final IDs on every child;
   do not invent fractional source timing.
9. Make the feature opt-in. A disabled cap must preserve the current one-parent,
   one-TTS-call behavior byte-for-byte.

Telemetry and integrity need separate parent and child accounting:

- `segments_emitted` and NMT completion remain parent counts;
- `tts_subsegments_produced` and audio output are composite-key counts;
- each TTS, output-dequeue, and WebSocket-send event records parent ID,
  subsequence ID, and subsequence count;
- a parent is complete only after its final child is dequeued;
- every parent has exactly `0..subsequence_count-1`, with no gaps, duplicates,
  or reorder;
- sent and received PCM frame counts and bytes match exactly; and
- one terminal follows all PCM.

Legacy captures without child fields should be interpreted as `(sequence, 0,
1)`. This preserves analysis compatibility while the new telemetry schema is
introduced.

## Next live experiment

Do not run another three-file matrix first. Implement the splitter and composite
integrity contract under a default-off feature flag, then run one real-time
five-minute canary for each policy against the same consent-cleared source
prefix:

1. splitting disabled;
2. 40-character strict cap;
3. 45-character near-boundary cap; and
4. 60-character efficiency cap.

Use identical pinned models, 800 ms EOU, upstream punctuation splitting, queue
sizes, retry bounds, and playback policy. Reject any candidate with missing,
duplicate, reordered, or corrupted PCM, terminal failure, or an unrecovered
model error.

For every arm record:

- parent translations and TTS subsegment calls;
- actual subsegment-duration p50/p95/max;
- output/input duration ratio and change from the unsplit control;
- first audio, service tail, listener queue p50/p95/peak, and listener tail;
- time above 10 seconds and playback-rate exposure;
- TTS retry/failure counts and per-call processing overhead; and
- same-clock source-event to translated-audible delay when a semantic marker is
  available.

Promote at most two candidates to a longer replay. Only after one candidate
reduces bursts without materially worsening total expansion should the complete
three-sample matrix be repeated. Native-Spanish review remains required because
request boundaries can change pauses, prosody, and intelligibility.

## Reproduction

Run the analyzer against the completed matrix summaries without contacting the
GPU services:

```bash
PYTHONPATH=.python-packages:backend:. python3 analyze_tts_duration.py \
  --input-dir \
    experiment_results/post-tts-recovery-matrix-20260724T054450Z-636f478/repeat-01 \
  --json-output \
    experiment_results/post-tts-recovery-matrix-20260724T054450Z-636f478/tts_duration_model.json \
  --markdown-output \
    experiment_results/post-tts-recovery-matrix-20260724T054450Z-636f478/tts_duration_model.md
```

Both generated files stay inside the ignored experiment directory. The
checked-in analyzer, tests, this transcript-free report, and the structural
digest make the decision reproducible when the private runtime artifacts are
restored.
