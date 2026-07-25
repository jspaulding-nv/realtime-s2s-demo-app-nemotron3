# Semantic event gate long-form diagnostic — July 25, 2026

## Result and claim boundary

The long-form browser run completed through ASR, NMT, TTS, and projected
playback, but the semantic event gate result is **INVALID evidence**. It is
neither a semantic PASS nor a semantic FAIL.

The capture exposed two independent evidence defects:

1. three translated parents lacked an exact word-derived source start; and
2. the browser's client-monotonic and AudioContext clocks crossed the
   pre-registered linkage limits after a discrete clock-offset step.

No semantic marker sidecar was created. No source event was independently
marked or reviewed by two people, and no target-language semantic landmark was
identified. The measurements below therefore describe mechanical pipeline and
browser scheduling behavior only. They do not prove rendered-device output or
physical audibility.

## Runtime and pinned services

- GPU: NVIDIA RTX PRO 6000 Blackwell Server Edition
- Pipeline: direct staged ASR -> NMT -> TTS
- Input: 16 kHz mono Int16, 300 ms frames, absolute source-end pacing
- EOU: 800 ms
- ASR word timing: enabled
- TTS publication: schema-3 incremental, 100 ms frames
- Post-NMT TTS splitting: disabled
- Playback: adaptive no-drop 1.00x/1.05x/1.10x policy

All three services were healthy before capture:

| Stage | Image | Digest |
| --- | --- | --- |
| ASR | `nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0` | `sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850` |
| NMT | `nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2` | `sha256:3789b08b72c8dfbb09d1144e2bfd1f13c95911c2f997c9e11d81afb5aa90c9fb` |
| TTS | `nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0` | `sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d` |

## Frozen capture artifacts

The browser capture completed normally with 645 translated parents and 19,544
output frames. There was no fatal application, service, or transport error.
Artifacts remain in the ignored directory:

```text
experiment_results/semantic-event-gate/long-form-03-30min-headless-20260725T0640Z/
```

| Artifact | Evidence |
| --- | --- |
| Dashboard CSV | `timing-export-2026-07-25T07-11-38-625Z.csv` |
| Dashboard CSV SHA-256 | `292c260ab94e669c25c99962858bee2e19ded40021edbbe6d12fec8bc51c9728` |
| Dashboard CSV size | 14,723,841 bytes |
| Raw backend export | `backend-export.json` |
| Raw backend export SHA-256 | `67d13b7dd535c71dd76721d8ea415e53786062c9604fb2f458a1628cafc178c8` |
| Raw backend export size | 58,251,745 bytes |
| Tracked WAV SHA-256 | `2e6b394e7a68cd5d8c39bb332aeff0c790ba059fd4501c3a71a2635181e0f20a` |
| Padded wire PCM SHA-256 | `b3e622c467fb4be80622df5c2fea708cf08ca49a000cf46933bdadd0e1b9e11b` |
| Padded wire PCM | 30,211,200 samples / 1,888.200 s |
| Input chunks | 6,294 |

The WAV-file digest and padded PCM digest intentionally bind different byte
representations. The semantic analyzer binds a future private sidecar to the
exact padded Int16 PCM wire image, not to the encoded WAV bytes.

## Mechanical listener-queue finding

The adaptive no-drop controller was recoverable, but its 1.10x maximum playback
rate did not keep the projected listener queue within the intended 5-10 second
range during this run.

| Queue measurement | Result |
| --- | ---: |
| Time-weighted p95 queue | 18.88 s |
| Peak queue | 34.89 s |
| Time above 5 s | 1,361 s |
| Time above 10 s | 655 s |
| Queue-limit breach events | 106 |
| Queue still draining when input ended | 12.29 s |
| Projected tail after input ended | 15.401 s |

The 19,544 scheduling events produced a nearest-rank p95 queue of 19.581
seconds and a maximum of 34.891 seconds. Of those events, 15,254 were above
five seconds and 7,448 were above ten seconds. Scheduled frame counts by
playback rate were:

| Playback rate | Frames |
| --- | ---: |
| 1.00x | 4,049 |
| 1.05x | 4,582 |
| 1.10x | 10,913 |

For frames with source attribution, raw projected-start source-end lag ranged
from 1.501 to 40.506 seconds and raw projected-end source-end lag ranged from
1.601 to 40.514 seconds. These values are scheduling projections, not
word-aligned target-language latency or actual audibility.

Duration-only drift was sometimes near zero or negative while the projected
queue was materially positive. Duration balance is therefore not a substitute
for the listener-queue metric. Mechanically, the trace supports the concern
that a time-sensitive source moment could reach a translated listener tens of
seconds later during a queue burst. A semantic claim about any particular
moment still requires a valid capture and human review.

## Invalid evidence reason 1: incomplete source attribution

The analyzer rejected the capture at:

```text
CSV row 27910 (audio_received) lacks an attributed source range
```

Of 645 translated parents, 642 had complete source ranges. Of 19,544 output
frames, 19,493 had complete source ranges. The remaining three parents and 51
frames carried an `audio_processed` end offset but no exact word-derived source
start:

| ASR final ID | Nonempty text length | ASR final end | Parent ID | Parent source end | Frames |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 261 | 9 characters | 1,102,661.011 ms | 363 | 1,102,661.011 ms | 14 |
| 315 | 5 characters | 1,330,069.580 ms | 452 | 1,330,069.580 ms | 7 |
| 376 | 25 characters | 1,628,373.291 ms | 548 | 1,628,400.000 ms | 30 |

The first two parents used final IDs `[260, 261]` and `[315]`, respectively.
The third used final IDs `[376, 377]`; its later parent end reflects the
combined segment. In each affected final, the model returned nonempty text
without a word array from which an exact start could be derived. Nearby
interim results also did not provide a recoverable word start.

The capture correctly preserved the missing start instead of fabricating one.
An `audio_processed` end alone is non-semantic and is insufficient for a
source-event envelope. Filling these gaps from neighboring ranges after seeing
the result would create unsupported evidence.

## Invalid evidence reason 2: browser clock discontinuity

Both scheduling recurrences remained internally sound:

- maximum AudioContext recurrence error: 0 ms;
- maximum projected client-clock recurrence error: 0.001 ms; and
- actual interval overlaps: 0.

The cross-clock linkage did not remain within the limits registered before the
run:

| Cross-clock measurement | Observed | Pre-registered limit |
| --- | ---: | ---: |
| Maximum absolute per-frame wait-link residual | 254.4 ms | 25 ms |
| Capture-wide clock-offset span | 286.9 ms | 50 ms |

The failure was dominated by one discrete offset step near 17.3 minutes, not by
steady 30-minute drift. The client-monotonic clock advanced about 489 ms while
AudioContext time advanced about 232 ms, producing an offset change of about
257 ms. Before the step, the sampled offset was approximately 5,804-5,807 ms;
afterward it was approximately 6,064 ms. The absolute residual's p95 was 12.7
ms, but the 254.4 ms maximum still invalidates the capture under the registered
model.

For comparison, the matched 60-second preflight observed a 17.8 ms maximum
link residual and a 28.3 ms offset span. The long-form result shows why those
short-run limits cannot be loosened after observing a formal capture. The
evidence model must be revised and frozen before collecting replacement
evidence.

## Requirements registered for the next run

A fresh long-form run is required. Before starting it:

1. Freeze and review a long-duration browser clock model. It must either record
   a direct common-clock mapping, such as browser output-timestamp evidence, or
   apply a conservative, capture-wide guarded clock-offset interval. Its limits
   and decision-bound widening must be declared before examining the new run.
2. Add deterministic tests for a discrete client/AudioContext offset step,
   gradual divergence, render-quantized clock movement, queue underruns, and
   recurrences across parent boundaries. A clock discontinuity must widen
   bounds conservatively or reject the capture; it must never silently improve
   latency.
3. Preserve privacy-safe ASR final and segmentation provenance sufficient to
   explain every source envelope. Require exact word-derived starts for every
   semantic parent, or preregister and test a conservative treatment of
   end-only parents. Do not infer an exact start from neighboring ranges.
4. Keep the pinned service versions, staged schema-3 incremental path, 800 ms
   EOU, word timing, absolute source-end pacing, and adaptive playback policy
   unchanged unless a separate experiment explicitly declares a changed
   variable.
5. Complete a mechanical preflight with the revised analyzer, then collect one
   uninterrupted long-form browser capture. Freeze its CSV and SHA-256 before
   semantic review.
6. Create semantic markers only after the replacement capture passes every
   mechanical validator. At least two human reviewers must independently mark
   and agree on the same source event. Automated or agent review does not count.
7. Run the frozen evidence at both the five-second objective and ten-second
   soft ceiling. Report INVALID if any evidence prerequisite fails; never
   convert invalid evidence into a semantic PASS or FAIL.
8. Treat the queue experiment separately from semantic validation. If the
   no-drop 1.10x policy again exceeds ten seconds, preregister a stronger
   freshness policy and obtain human quality review before changing playback
   speed, compressing output, summarizing content, or dropping queued audio.

Even a mechanically valid semantic gate remains a browser scheduling
projection. Proving what a listener physically heard requires a separate
rendered-output or two-channel acoustic capture.
