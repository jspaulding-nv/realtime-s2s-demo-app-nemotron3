# Browser-independent formal matrix result — 2026-07-26

## Decision

The clean merged Nemotron 3 staged pipeline completed the one-minute preflight
and all three long-form captures without losing, duplicating, or reordering a
translated PCM frame. The adaptive 1.00x/1.05x/1.10x listener policy reduced
the aggregate fixed-rate tail by 76.2%, but every long-form sample still missed
the proposed five-second-p95 and ten-second-peak queue gates.

This is a valid measurement, not an operational test failure. The current
no-drop pipeline preserved transport integrity, exercised recovery, and
exposed queue behavior directly, but it does not yet provide a bounded 5–10
second live-audience experience over long-form speech.

## Frozen provenance

- Repository commit:
  `4ab2da9039d6678396368a072af3874894a25f48`
- Repository worktree: clean according to the manifest
- Run ID: `formal-headless-v1-20260726T053311Z-4ab2da9`
- ASR: digest-pinned Nemotron 3 streaming ASR 1.2.0, English profile,
  800 ms EOU, word-time offsets enabled
- NMT: digest-pinned Riva Translate 1.6B 1.5.2
- TTS: digest-pinned Magpie multilingual TTS 1.7.0
- Pipeline: staged telemetry schema 3, incremental TTS publication enabled,
  post-NMT TTS subsegmentation disabled
- Queue capacities: NMT 4, TTS 4, output 4
- Playback policy: target 5 s, urgent 8 s, limit 10 s; rates
  1.00x/1.05x/1.10x; no speech drops
- Inputs: the three long-form input SHA-256 values were manifest-bound and
  resume-validated
- Repeats: one live trace per sample, sequential

The manifest spans 6,424.997 seconds from creation through completion
(1 hour 47 minutes 5 seconds).

Separate operator checks before and after the run observed an NVIDIA RTX PRO
6000 Blackwell Server Edition with 97,887 MiB and all three NIM containers
healthy, with zero restarts and no OOM events. The harness does not manage or
snapshot Docker or GPU state, so these are unretained operational observations,
not manifest-bound or resume-validated provenance.

## Preflight

The 60-second protocol-v1 preflight passed:

- 200 of 200 source chunks sent at absolute real-time boundaries;
- 114 translated frames received and scheduled;
- single completed terminal reached;
- queue p95 4.072 s and peak 5.211 s;
- zero dropped, reordered, or duplicated frames; and
- 1.447 s service tail.

## Long-form results

| Metric | Sample 01 | Sample 02 | Sample 03 |
|---|---:|---:|---:|
| Source duration | 1,908.432 s | 2,427.011 s | 1,888.105 s |
| Translated PCM duration | 2,106.068 s | 2,608.416 s | 1,886.363 s |
| Translated/source duration delta | +10.36% | +7.47% | -0.09% |
| Protocol-v1 frames preserved | 4,499 | 5,609 | 4,101 |
| Completed parents | 581 | 799 | 640 |
| NMT retries recovered | 0 | 3 | 0 |
| TTS retries | 0 | 0 | 0 |
| Fixed 1.00x listener tail | 240.550 s | 213.061 s | 49.663 s |
| Adaptive listener tail | 80.876 s | 28.938 s | 9.936 s |
| Tail reduction | 66.4% | 86.4% | 80.0% |
| Adaptive queue p95 | 71.642 s | 32.745 s | 21.525 s |
| Adaptive peak queue | 83.052 s | 40.478 s | 34.474 s |
| Playback time above 10 s | 77.22% | 76.49% | 35.72% |
| Source-end to first arrival p95 | 5.773 s | 5.536 s | 4.765 s |
| Source-end to scheduled start p95 | 74.302 s | 35.561 s | 24.090 s |
| First-to-final-quartile start drift | +54.312 s | +6.910 s | +11.275 s |
| Candidate 5–10 s queue gate | **MISS** | **MISS** | **MISS** |

Across the three samples, translated PCM totaled 6,600.85 seconds for
6,223.55 seconds of source audio, an aggregate increase of about 6.06%.
Listener tail fell from 503.274 seconds at fixed 1.00x playback to 119.750
seconds with the adaptive policy.

## What the result establishes

### The transport and recovery design worked

Every one of the 14,209 translated frames was retained once and in order. All
2,020 parents reached completion. Bounded queue backpressure occurred without
losing work. Sample 02 exercised the configured NMT recovery path three times;
all three retries succeeded and the capture completed. No TTS retry, container
terminal timeout, or artifact-integrity failure occurred. The separate
operator checks described above observed no container restart or OOM event.

### Nemotron 3 is viable; protocol-v1 offsets improve coarse observability

Nemotron 3 completed all three English streaming inputs and supplied bounded
ASR source ranges for 570/581, 778/799, and 627/640 parents respectively
(about 97–98% coverage). Together with protocol-v1 instrumentation, the
enabled offsets support coarse source-frontier latency and accumulated-drift
metrics rather than relying only on whole-file duration or a final tail.

The separate timing-shape qualification found incomplete and point-like word
entries, so these ranges are not qualified semantic word landmarks. Nemotron
3's English streaming profile, punctuation handling, and 800 ms EOU remain a
viable ASR foundation, but this run was not an A/B comparison against the
former ASR. It neither assigns a percentage improvement to the model
substitution nor establishes exact word alignment.

### Translated-media duration matters, but it is not the only cause

Samples 01 and 02 produced 10.36% and 7.47% more translated audio than source
audio in this one trace each. The ratios can reflect translated text length,
language expansion, voice pacing, silence, and TTS behavior; this run does not
isolate one cause or establish stable per-sample ratios. Sample 01's ratio
slightly exceeded the listener policy's 1.10x maximum rate. A no-drop listener
at constant 1.10x therefore cannot catch up to the live source by end-of-input
on that trace, although it drains the finite backlog after input stops.

Sample 03 is the important counterexample: its translated and source durations
were essentially equal, yet its queue p95 was 21.525 seconds and its peak was
34.474 seconds. Nonuniform end-to-end delivery cadence therefore created
substantial listener backlog even when whole-file media expansion was zero.
Stage attribution is still required before assigning that residual to ASR,
NMT, TTS, or transport behavior.

### The live-audience concern remains valid

The adaptive scheduled-start p95 values were 74.302, 35.561, and 24.090
seconds. These are coarse ASR-parent source-range projections, not reviewed
joke or punchline landmarks, physical audibility, or measured audience
reaction. They nevertheless show that a listener can be tens of seconds
behind the live source after backlog accumulates. That demonstrates a strong
risk that reactions to time-sensitive speech would be noticeably asynchronous.

## Offline capacity sweep

The saved arrival traces were replayed without dropping speech at constant
playback rates from 1.10x through 1.25x. A second dimension multiplied every
translated PCM chunk's duration by 1.000000, 0.909091, 0.85, or 0.80 while
leaving its captured arrival timestamp and the source input boundary
unchanged. The duration scale is a counterfactual; it does not establish a
supported TTS prosody control, listening quality, or the live service timing
that such a control would produce.

Faster playback alone did not pass either queue gate:

| Playback rate, unchanged media | Sample 01 p95 / peak | Sample 02 p95 / peak | Sample 03 p95 / peak |
|---|---:|---:|---:|
| 1.15x | 41.678 / 53.539 s | 24.289 / 30.917 s | 17.931 / 31.950 s |
| 1.20x | 33.948 / 41.592 s | 19.879 / 28.278 s | 14.936 / 30.259 s |
| 1.25x | 27.101 / 36.151 s | 16.586 / 25.851 s | 12.567 / 28.704 s |

Constant maximum-rate playback is an optimistic queue lower bound for any
adaptive no-drop policy with the same ceiling. The 1.25x result therefore also
rules out an adaptive policy capped at 1.25x on these unchanged traces.

Even the most aggressive tested counterfactual—1.25x playback with every
translated chunk shortened by 20%—missed:

| Metric at 1.25x playback and 0.80 media scale | Sample 01 | Sample 02 | Sample 03 |
|---|---:|---:|---:|
| Queue p95 | 11.674 s | 8.450 s | 6.306 s |
| Peak queue | 25.569 s | 16.737 s | 21.236 s |
| No-drop listener tail | 8.273 s | 0.802 s | 4.281 s |
| Candidate 5–10 s queue gate | **MISS** | **MISS** | **MISS** |

Captured translated-media arrival rates were also bursty. The p95 rates over
30-second windows were 1.652x, 1.485x, and 1.395x real time for Samples 01,
02, and 03; over 300-second windows they were 1.206x, 1.165x, and 1.082x.
This explains why shrinking total media or raising average playback capacity
alone does not guarantee a bounded instantaneous queue.

## Recommended next experiment

The offline sweep rules out a playback-rate increase through 1.25x as a
sufficient isolated fix. Before another full three-sample GPU run:

1. **Attribute queue-growth windows by stage.** Use the retained schema-3
   events to align ASR finalization and parent emission, NMT queue residence
   and processing, TTS first audio, incremental frame publication, and blocked
   queue puts with the worst listener-backlog windows. This is an offline
   analysis of the completed run.
2. **Run a short controlled TTS-duration canary for lever isolation.** First
   confirm a supported rate or prosody control; the pinned staged client's
   current direct TTS request exposes no rate field. If a control is available,
   test a setting near 1.10x and obtain native-language quality review. The
   saved sweep already shows this is not a likely standalone gate candidate;
   the canary would determine whether live synthesis duration and response
   cadence change together.
3. **Improve publication cadence based on the attribution.** Reduce long
   arrival gaps or parent bursts at the responsible stage. Do not re-enable
   the previously rejected post-NMT subsegmentation policies without new
   evidence; those policies increased generated audio in prior canaries.
4. **Gate cheaply before repeating the matrix.** Repeat the one-minute
   preflight and a five-minute no-drop canary. Run all three long-form samples
   only if p95 and peak queue move materially toward five and ten seconds.
5. **Keep the no-drop constraint explicit.** A strict ten-second hard cap is
   mathematically impossible when cumulative translated-media production over
   an interval exceeds maximum playback capacity plus the available buffer
   headroom, unless the design permits summarization, omission, or
   coordination with the live speaker.
6. After the mechanical queue gate is close, record reviewed source and
   target-language landmarks on a common clock to measure actual
   phrase/punchline delay and conduct listening-quality review.

## Artifact validation and custody

The raw owner-private artifact directory is:

```text
experiment_results/formal-headless-v1-20260726T053311Z-4ab2da9/
```

It is approximately 119 MiB and contains the manifest, preflight, three
CSV/summary/plot bundles, and schema-2 cross-sample analysis. The runner's
resume path revalidated the clean commit, frozen backend/model configuration,
the three long-form input SHA-256 values, artifact SHA-256 values, counters,
byte totals, protocol identity, parent/frame ordering, source/receipt clock
equations, and exact headless scheduler replay. All completed checkpoints were
accepted and skipped.

The post-run capacity sweep is stored beside those artifacts as
`playback_capacity_sweep.json` and `playback_capacity_sweep.md`. Their SHA-256
values are `b0367bec704186480ed8b9f6a8db252802d80a75f7f4d13ecbec2ee8f14d288d`
and `112c794ea7849c8b57eddba2d28b4bd59de27bdea488281c360d8f92385e681c`
respectively.

Raw traces remain ignored by Git and should be copied to approved private
storage before the VM lease ends.
