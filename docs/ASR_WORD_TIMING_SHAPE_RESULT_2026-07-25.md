# ASR Word-Timing-Shape Diagnostic Result — 2026-07-25

## Outcome and claim boundary

**COMPLETE diagnostic — not qualification evidence.**

One registered real-time replay of the exact tracked long-form input completed
with valid input binding, pacing, client configuration, and runtime continuity.
It reproduced the same 13 incomplete final IDs seen in both earlier formal
qualification runs and explained their raw response shapes.

This result does not pass the formal ASR attribution gate, measure
transcription accuracy, or measure end-to-end audience latency. The formal gate
still requires two complete runs with zero nonempty finals lacking a supported
source envelope.

## Registered capture

| Item | Observed value |
|---|---|
| ASR image | `nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0` |
| ASR repository digest | `sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850` |
| ASR profile selector hash | `8d3fa26a44c471552b7edac76372f3db66e5c1bd73e24abac701091717026c58` |
| Language / EOU | `en-US` / 800 ms |
| Word offsets requested | Yes |
| Source WAV SHA-256 | `2e6b394e7a68cd5d8c39bb332aeff0c790ba059fd4501c3a71a2635181e0f20a` |
| Source / padded samples | 30,209,672 / 30,211,200 |
| Padded PCM SHA-256 | `9ad08fe1e83e714c48dde4f971606431302fae9d3079293b73ab8ff99d1d8143` |
| Audio sent | 1,888.2 seconds |
| Wall time | 1,888.578 seconds |

The source WAV hash matched before, during, and after capture. The exact padded
PCM binding passed. The ASR container stayed healthy with the same immutable
image and privacy-safe instance fingerprint before and after capture.

Real-time pacing also passed:

| Metric | Result |
|---|---:|
| Chunks released | 6,294 |
| Maximum release lateness | 7.003 ms |
| Mean release lateness | 0.461 ms |
| Interim hypotheses | 9,763 |

## Final-envelope result

| Final-envelope shape | Count | Share of 437 nonempty finals |
|---|---:|---:|
| Positive-duration envelope | 424 | 97.025% |
| Zero-duration envelope | 10 | 2.288% |
| No word entries | 3 | 0.686% |
| Absent, unparseable, nonfinite, negative, or reversed | 0 | 0.000% |

The 13 affected final IDs exactly match both prior formal runs:

```text
98, 179, 214, 215, 237, 261, 274, 295, 296, 302, 315, 376, 404
```

This third deterministic reproduction makes a VM scheduling race or transient
client failure an implausible explanation.

## What the raw shapes show

The important finding is narrower than the earlier hypotheses:

- No supplied start or end scalar was numerically zero.
- No supplied scalar was absent, unparseable, nonfinite, or negative.
- No word entry or result envelope was reversed.
- All 5,263 supplied start scalars and all 5,263 supplied end scalars were
  positive numbers. Presence remains `unobservable` because the installed
  proto3 scalar fields do not expose presence.
- Of 5,263 word entries, 4,676 had equal positive start and end timestamps
  (`zero_length`) and 587 had positive duration.

The per-entry distribution was therefore:

| Word-entry shape | Count | Share |
|---|---:|---:|
| Equal positive start/end | 4,676 | 88.847% |
| Positive duration | 587 | 11.153% |
| All other shapes | 0 | 0.000% |

Most finals still had a usable result envelope because the first and last word
timestamps were different even when individual word entries were
zero-duration. Of the 424 positive-duration final envelopes, 408 still
contained at least one zero-duration word entry. The ten incomplete
word-bearing finals were different:

- nine contained one word entry whose positive start and end were equal; and
- one contained two entries whose first start and last end were the same
  positive timestamp.

For those ten finals, the ASR `audio_processed` horizon was 805.786–1,407.949
ms after the equal word timestamp, with a mean difference of 989.604 ms. That
interval is recorded only as a processing horizon. Without a service contract,
it must not be relabeled as an exact semantic word end.

The other three finals (`261`, `315`, and `376`) had nonempty final text but no
word entries. They retain only an `audio_processed` end horizon and still have
no word-derived start.

## Interpretation

The data are consistent with an interpretation in which many returned word
times act as alignment points rather than conventional positive-duration word
intervals. That is an inference from the observed shape, not a statement of
the service contract.

The current formal gate's positive-duration-envelope requirement is therefore
stricter than the raw response shape for ten point-like finals. Relaxing it
without confirmation would be post hoc and would still leave the three
no-word-entry finals unresolved.

An unchanged two-run qualification should not be repeated now. It would be
expected to reproduce the same deterministic failure.

## Recommended next decision

Send this transcript-free evidence package to the ASR service team and ask:

1. Are Nemotron streaming `WordInfo.start_time` and `end_time` intended to be
   duration boundaries or point-alignment timestamps?
2. Is equal positive start/end expected for word entries, including an entire
   one-word final?
3. Is a nonempty final with no word entries expected when word offsets are
   requested?
4. What supported source boundary should a client use for point-like finals?
   In particular, what semantic guarantee, if any, does `audio_processed`
   provide?
5. Is there a supported model profile, client field, or server change that
   returns complete semantic boundaries for every nonempty final?

Then take one preregistered path:

- If positive-duration intervals are promised, obtain the supported
  configuration or service fix and rerun the unchanged formal gate.
- If point-alignment timestamps are the contract, specify and test a new
  point-event attribution policy before applying it. Such a policy could
  address the ten zero-duration envelopes but not the three no-word-entry
  finals.
- For no-word-entry finals, require service-provided timing or separately
  preregister a conservative parent policy. Do not infer an exact source start
  from neighboring finals after observing the result.

Resolving source attribution enables the formal semantic audience-latency
measurement; it does not solve queue growth. The
[prior mechanical long-form browser result](SEMANTIC_EVENT_GATE_LONG_FORM_DIAGNOSTIC_2026-07-25.md)
still showed a time-weighted p95 listener queue of 18.88 seconds and a
34.89-second peak under the no-drop 1.10x policy. The 5–10 second
audience-freshness objective therefore remains a separate, necessary control
problem after the evidence contract is settled.

For the product question, run a parallel path that does not depend on every ASR
final having a word array: preregister a same-clock capture of the source audio
and the actually rendered translated output. Select multiple source events
across the beginning, middle, and end without looking at output timing, and
have two bilingual reviewers independently identify the corresponding
translated landmarks. An unidentifiable landmark must be `INCONCLUSIVE`, not
silently omitted.

Before that rendered-output gate, preregister the listener-queue requirement.
A defensible starting proposal is a time-weighted p95 no greater than 5 seconds
and a maximum no greater than 10 seconds, with no unreported loss or sustained
queue growth. Any stronger catch-up, compression, or loss policy needed to
reach that bound requires a separate intelligibility and translation-quality
review. This common-clock path directly addresses the live-audience delay
question while the ASR metadata contract is resolved in parallel.

## Privacy-safe artifact

The full JSON remains ignored from Git:

```text
experiment_results/asr-word-timing-shape-diagnostic-20260725.json
```

Artifact size: 3,636,527 bytes.

Artifact SHA-256:

```text
b4d37272353e90a9a06481298a9f4c23664d399a67c22d47a535a0a7dfb5adff
```

The artifact passed the strict report validator. It contains no transcript,
translation, word or token text, audio, arbitrary path, endpoint, local
container name, speaker identity, or organization identity.
