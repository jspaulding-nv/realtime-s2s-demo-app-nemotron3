# Audio metadata protocol v1: 60-second formal canary

## Decision

Audio metadata protocol v1 passed its first formal live gate from clean commit
`062f28a`.

The atomic control remained on legacy anonymous PCM. The schema-3 incremental
arm negotiated v1 and reconciled all 521 `audio_frame`/PCM pairs and all 16
parent completions. Both arms completed 200 real-time input chunks without a
connection loss, drain timeout, server error, or staged-integrity error. The
three pinned Riva NIM containers remained healthy after the run.

This promotes v1 to the five-minute observation gate. It does not promote live
audio dropping or claim that the audience-delay objective is solved.

## Matched configuration

- one shared 60-second, 16 kHz mono prefix;
- Nemotron ASR streaming `1.2.0`;
- Riva Translate `1.5.2`;
- Magpie multilingual TTS `1.7.0`;
- 800 ms ASR end-of-utterance history;
- ASR word-time configuration disabled;
- atomic schema 1 versus incremental schema 3;
- 100 ms incremental PCM frames;
- four-character atomic reliability fallback; and
- identical ASR-final, segment, NMT-parent, input, model, and playback-policy
  structure across arms.

The formal comparator accepted all intended configuration differences and
rejected every unplanned difference.

## Operational integrity

| Evidence | Atomic control | Incremental v1 |
|---|---:|---:|
| Input chunks | 200 | 200 |
| NMT/TTS parents | 16 | 16 |
| WebSocket audio messages | 16 | 521 |
| Metadata frame pairs | 0 | 521 |
| Metadata parent completions | 0 | 16 |
| Direct incremental parents | 0 | 15 |
| Atomic-fallback parents | 0 | 1 |
| Clean terminal and drain | yes | yes |
| Staged integrity | passed | passed |

The control recorded no protocol version, generation, source-clock anchor,
frame, parent, or freshness samples. This proves the opt-in remained scoped to
the incremental arm.

## Direct incremental-publication result

Across the 15 genuinely incremental parents:

- first PCM followed the first TTS response by 34.7 ms p50 / 58.8 ms p95; and
- first PCM led full TTS completion by 345.4 ms p50 / 1.525 s p95.

First translated audio arrived at 15.504 seconds in the atomic arm and 15.160
seconds in the incremental arm, a nominal 344 ms improvement.

The atomic and incremental arms generated 52.803 and 51.224 seconds of audio,
respectively. Their 2.99% difference exceeds the comparator's 1% materiality
threshold. Cross-arm queue, tail, and first-audio deltas therefore remain
confounded by stochastic TTS output. The within-arm publication measurements
above are the primary evidence.

## Source-clock freshness observation

Although the configured ASR word-time switch was off, the captured finals
contained source ranges. The analyzer classified the observed end offsets as
ASR source-range ends.

For all 521 translated PCM frames:

| Observation | p50 | p95 | Maximum |
|---|---:|---:|---:|
| Source end to client PCM receipt | 2.656 s | 3.915 s | 4.233 s |
| Source end to deterministic scheduled start | 8.264 s | 12.860 s | 14.338 s |

These are same-client-monotonic measurements from input PCM sample zero. A
source-range end is not a proven phrase or punchline boundary. Deterministic
scheduled start is not proof of DAC output or physical audibility.

The distinction is important for a live audience: transport/inference
freshness was under 4 seconds at p95, but queued playback moved the projected
start beyond the 5–10 second audience objective at p95. Even this short run
therefore preserves the concern that a translated reaction moment can occur
noticeably after the source moment.

## Queue and shadow-policy result

The unchanged adaptive no-drop replay produced:

- 9.383-second time-weighted queue p95;
- 11.317-second peak queue;
- 4.746-second listener tail; and
- 3.28% of its playback window above 10 seconds.

The observation-only 10-second oldest-first whole-parent policy would have:

- retained 87.3% of translated audio;
- skipped 1 of 16 complete parents;
- reduced queue p95 to 8.317 seconds; but
- still peaked at 11.317 seconds and spent 0.319 seconds above the cap.

The same headline held for 0, 50, 100, and 250 ms cancellation guards. No live
audio was removed. The result confirms that whole-parent eviction cannot
guarantee a hard limit when the remaining audio is already playing,
incomplete, protected, or itself long.

## Formal-gate correction

The first clean candidate run from `4959b17` completed both model arms, but the
final comparator rejected it. The batch client had recorded an input
sample-zero marker in the legacy atomic artifact even though v1 was not
negotiated.

Commit `062f28a` scopes that anchor to negotiated streams and adds a legacy
regression test. Seventy-three focused capture and comparator tests passed
before the successful rerun. The rejected candidate is diagnostic evidence,
not a formal result.

## Next gates

1. Repeat the matched observation canary with a five-minute shared prefix.
2. Run all three long-form samples with
   `run_long_form_experiment.py --audio-metadata-protocol-v1`.
3. Compare source-end receipt, projected scheduled start, queue, tail,
   hypothetical loss, and residual breaches across all samples.
4. Add a synchronized semantic marker and translated-output loopback for a
   defensible phrase/punchline-to-audibility measurement.
5. Review 1.05x/1.10x output quality with native listeners.
6. Only then test a separately gated short-lookahead scheduler.

The working audience target remains a bounded queue of roughly 5–10 seconds.
Protocol v1 makes progress toward that target measurable; it does not enforce
the target.
