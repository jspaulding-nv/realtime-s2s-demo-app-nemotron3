# Staged recovery three-sample matrix: 2026-07-24

## Outcome

The clean, provenance-frozen staged pipeline completed one real-time capture
for each of the three neutral long-form samples. The run passed its preflight,
operational, sequence, PCM-parity, terminal, artifact-hash, and model-provenance
gates:

- 2,027 of 2,027 emitted segments produced ordered translated audio;
- no incomplete sequence, connection loss, drain timeout, server failure,
  or cleanup failure occurred;
- three guarded NMT retries completed successfully;
- no TTS retry was needed; and
- all three captures completed from one clean Git commit without resume.

This is an operational pass and an audience-latency miss. Deterministic replay
of each live arrival trace through the 1.00x/1.05x/1.10x adaptive policy reduced
the sum of listener tails by 77.677%, from 526.441 seconds to 117.516 seconds,
without dropping audio. The adaptive time-weighted queue p95 was still
61.414, 38.158, and 23.210 seconds. Every trace exceeded the proposed
5-second target and 10-second soft ceiling.

Consequently, a time-sensitive moment such as a joke can still be heard by the
translated-audio audience tens of seconds after the source-language audience,
even when the service itself finishes within a few seconds of end-of-input.

## Evidence boundary

The ignored run directory contains the manifest, event CSVs, summary JSON,
plots, and deterministic playback analysis. Those artifacts retain local
runtime metadata and are not committed. This document retains only aggregate,
transcript-free measurements.

The capture and analysis semantics are:

- one new live Riva trace per sample, paced in real time;
- fixed and adaptive schedules calculated from the same trace;
- deterministic Python playback simulation, not executed browser Web Audio;
- no native-listener translation, prosody, or speed-quality evaluation; and
- no synchronized source-event-to-translated-audible semantic marker.

Queue depth therefore quantifies one part of audience delay. It does not by
itself measure the exact source punchline to translated punchline delay.

## Frozen provenance

| Field | Value |
|---|---|
| Git commit | `636f4784797372a8b6092255d0438951f9300c0b` |
| Git state | clean |
| Run status | completed |
| Preflight | completed and promoted |
| Pipeline | staged direct ASR -> NMT -> TTS |
| Source / target | `en-US` / `es-US` |
| ASR EOU | 800 ms |
| Segment bound | 240 characters or 2,000 ms |
| NMT / TTS / output queue capacity | 4 / 4 / 4 |
| TTS retry bound | one retry for genuine gRPC `UNKNOWN` only |

| Stage | Pinned image | Verified digest |
|---|---|---|
| ASR | `nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0` | `sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850` |
| NMT | `nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2` | `sha256:3789b08b72c8dfbb09d1144e2bfd1f13c95911c2f997c9e11d81afb5aa90c9fb` |
| TTS | `nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0` | `sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d` |

An immediate, separate post-run operational check reported 32,222 MiB used and
65,029 MiB free on an NVIDIA RTX PRO 6000 Blackwell Server Edition with
97,887 MiB total. It also found all three containers healthy with zero restarts
and `oom=false`. That check was observed outside the experiment manifest and is
not an artifact-hash-validated run gate. It confirms that the selected profiles
fit simultaneously on this 96 GB GPU; it does not make a separate throughput
guarantee for other GPU configurations.

## Operational results

`Audio tail` is the final translated-audio arrival after source end, floored at
zero. `Terminal tail` is the completed status after source end. Neither is the
listener playback tail.

| Metric | Sample 01 | Sample 02 | Sample 03 |
|---|---:|---:|---:|
| Input duration | 1,908.432 s | 2,427.011 s | 1,888.105 s |
| Translated PCM duration | 2,068.592 s | 2,630.336 s | 1,951.332 s |
| Output/input ratio | 1.083922x | 1.083776x | 1.033487x |
| Duration expansion | 8.392% | 8.378% | 3.349% |
| First translated audio | 15.508 s | 4.848 s | 5.170 s |
| Audio tail | 0.000 s | 2.086 s | 0.984 s |
| Terminal tail | 0.913 s | 3.197 s | 1.908 s |
| Completed segments | 580 | 804 | 643 |
| NMT retries | 0 | 3 | 0 |
| TTS retries | 0 | 0 | 0 |

Across the matrix, 6,223.547 seconds of source produced 6,650.260 seconds of
translated PCM: a weighted 6.856% expansion and 426.712 seconds of additional
media. All stage IDs were contiguous and identical through segment emission,
NMT, TTS, output dequeue, WebSocket send, and client receive. Successful PCM
send and receive counts and bytes matched exactly. Each capture had one
completed terminal after end-of-input and no PCM afterward.

The bounded queues reached maximum work-item depths of 4/4/1 for NMT/TTS/output.
Backpressure blocked NMT puts 15, 17, and 15 times and TTS puts 6, 2, and 1
times. No output put blocked, and no work was dropped or reordered.

## TTS recovery interpretation

The previously observed short-target Magpie fault did not recur in this
matrix, so the formal run did not itself exercise a TTS retry. The formerly
failing sample completed, and the run shows no observed operational regression
in this matrix with the retry-enabled adapter.

The recovery path itself was exercised separately before this matrix by a
privacy-safe exact-target probe: all 20 retry-enabled calls completed and two
reported one successful client retry. Preserve both claims:

- the targeted probe demonstrates live atomic retry recovery; and
- this matrix demonstrates clean long-form completion with the recovery
  enabled.

Do not claim that one three-sample matrix statistically eliminates the
intermittent service fault.

## Audience playback results

The controller used a 5-second target, an 8-second urgent threshold, a
10-second soft limit, and 1.00x/1.05x/1.10x playback. It never drops audio.

| Metric | Sample 01 | Sample 02 | Sample 03 |
|---|---:|---:|---:|
| Fixed 1.00x listener tail | 201.888 s | 228.357 s | 96.196 s |
| Adaptive listener tail | 67.790 s | 29.873 s | 19.854 s |
| Adaptive queue p50 | 21.582 s | 19.961 s | 9.132 s |
| Adaptive queue p95 | 61.414 s | 38.158 s | 23.210 s |
| Adaptive peak queue | 73.109 s | 46.949 s | 34.709 s |
| Playback time above 10 s | 76.352% | 77.790% | 45.760% |
| Translated media played at 1.10x | 93.061% | 94.811% | 78.650% |
| Longest continuous 1.10x interval | 1,001.169 s | 1,621.233 s | 314.612 s |
| Dropped chunks | 0 | 0 | 0 |

The controller was already at its 1.10x ceiling for most translated media.
Changing only the queue thresholds is therefore unlikely to establish a
5-10 second bound. For Samples 01 and 02, 1.10x has little average drain
headroom over approximately 1.084x media expansion, and arrival bursts can
create backlog faster than that small margin can remove it.

Whole-file expansion understates those bursts. Using one-second wall-clock
starts and fully contained half-open `[s, s + window)` intervals before
end-of-input, translated-media arrival over 60-second windows had p95 rates of
1.371x, 1.331x, and 1.269x relative to wall time. The corresponding five-minute
p95 rates were 1.210x, 1.167x, and 1.120x. Captured translated-audio chunks,
which map one-to-one to staged atomic TTS results in this run, were
approximately 7.2-8.8 seconds at p95 and 16.4-17.1 seconds at maximum. One
large completed segment can therefore exceed the 10-second queue objective
even if the listener queue was previously empty.

No-drop constant-rate replay confirms that this is a capacity limit:

| Constant rate | Sample 01 queue p95 / peak / tail | Sample 02 queue p95 / peak / tail | Sample 03 queue p95 / peak / tail |
|---|---:|---:|---:|
| 1.05x | 108.38 / 120.69 / 120.22 s | 109.75 / 126.28 / 117.64 s | 35.14 / 45.15 / 39.58 s |
| 1.10x | 61.41 / 73.11 / 67.79 s | 37.93 / 46.72 / 29.65 s | 22.98 / 34.44 / 19.85 s |
| 1.15x | 40.70 / 53.08 / 41.02 s | 20.36 / 29.13 / 16.15 s | 19.38 / 32.57 / 13.50 s |

No tested constant rate at or below 1.15x reaches queue p95 at or below
10 seconds on any trace. Diagnostic rates of approximately 1.678x, 1.453x,
and 1.432x would be required to reach that threshold on these traces. Those
values are capacity evidence, not acceptable playback recommendations.

A duration-only counterfactual also kept every chunk and held arrival times
fixed. At constant 1.10x playback, multiplying every translated chunk by
`0.952381` (the duration equivalent of 1.05x production) yielded queue p95 of
39.501, 20.056, and 19.119 seconds. Scale `0.909091` (the duration equivalent
of 1.10x production) yielded 30.813, 17.306, and 16.560 seconds. This is not a
claim that the pinned TTS model supports those prosody controls, and it does
not model changes in synthesis completion time or listening quality.

Reproduce the checked-in sweep without contacting the GPU services:

```bash
PYTHONPATH=.python-packages:backend:. python analyze_playback_policy.py \
  --input-dir \
    experiment_results/post-tts-recovery-matrix-20260724T054450Z-636f478/repeat-01 \
  --json-output \
    experiment_results/post-tts-recovery-matrix-20260724T054450Z-636f478/playback_capacity_sweep.json \
  --markdown-output \
    experiment_results/post-tts-recovery-matrix-20260724T054450Z-636f478/playback_capacity_sweep.md \
  --constant-rate 1.00 \
  --constant-rate 1.05 \
  --constant-rate 1.10 \
  --constant-rate 1.15 \
  --media-duration-scale 1.0 \
  --media-duration-scale 0.952381 \
  --media-duration-scale 0.909091
```

The generated sweep remains ignored with the raw trace. Without the optional
rate flags, the analyzer's JSON and Markdown output remain byte-compatible
with the prior default behavior.

The long continuous 1.10x intervals also make native-Spanish quality review a
required gate. Tail reduction alone is not sufficient evidence that sustained
accelerated speech is acceptable.

## Software validation

- Full Python backend, analysis, and harness suite: 413 passed, 1 skipped.
- Capacity-analyzer focused suite: 24 passed, 1 optional local-trace test
  skipped.
- The analyzer's default JSON and Markdown were compared byte-for-byte with
  commit `636f478`; both were identical when the new sweep flags were absent.
- Independent review matched the wall-clock rolling-rate implementation
  against a brute-force calculation over 10,000 randomized cases.
- Diff, link, privacy, and generated-artifact boundary checks passed.

## Recommended next experiment

Do not immediately spend another full matrix on the unchanged policy. Use the
three completed arrival traces first:

1. Use the checked-in deterministic constant-rate and media-duration sweep to
   reproduce the capacity result. Extend the experiment to adaptive maximum
   rates through 1.12x and 1.15x. Treat values above 1.10x as exploratory until
   listening quality is reviewed.
2. Report queue p50/p95/peak, time above 10 seconds, tail, rate exposure, and
   longest continuous accelerated interval for every candidate. Reject any
   policy that drops translated chunks.
3. Instrument leading, trailing, and internal low-energy PCM duration without
   retaining generated audio. This determines whether bounded pause
   compression can remove delay with less quality impact than faster speech.
4. Test smaller atomic segmentation bounds, beginning with 160 and 120
   characters while retaining 800 ms EOU and the existing age fallback.
   Recheck translation quality and short-segment safety before any full run.
5. Confirm server-side prosody support before implementation. The pinned
   staged client's direct TTS request exposes no rate field; any supported
   control must be added explicitly to configuration, provenance, telemetry,
   duration validation, and listening-quality tests.
6. Add synchronized source-event and translated-audible markers, then measure
   semantic event delay. This is the direct test for the live-joke scenario.
7. Cross-check the selected candidate in actual browser Web Audio and obtain
   native-Spanish review at 1.05x, 1.10x, and any higher exploratory rate.
8. Only then repeat the live three-sample matrix. Use three repeats per sample
   after a candidate approaches the 5-10 second queue objective.

If 10 seconds must become a hard bound, the product policy must explicitly
define how to react when no-drop playback cannot drain fast enough. Do not
silently discard translated speech. Prefer sufficient catch-up capacity,
validated TTS prosody or pause compression, and measured quality before
considering any content-loss policy.
