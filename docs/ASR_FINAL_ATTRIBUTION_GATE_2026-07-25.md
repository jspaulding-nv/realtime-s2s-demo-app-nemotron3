# ASR Final-Attribution Qualification — 2026-07-25

## Outcome

**BLOCKED — the ASR attribution prerequisite did not pass.**

Both registered real-time replays completed with valid runtime, input, and
pacing evidence. The gate returned `passed=false` because 13 of 437 nonempty
ASR finals in each run lacked a complete, usable word-time envelope. The same
13 final IDs and timing-basis classifications repeated in both runs.

This result blocks a formal semantic source-event browser gate. It does not
measure transcription accuracy, translation quality, target-language
audibility, or end-to-end audience latency by itself.

The formal gate remains failed. The next registered action is one
diagnostic-only real-time replay using the privacy-safe raw word-timing-shape
instrumentation described in
[ASR Word-Timing-Shape Diagnostic](ASR_WORD_TIMING_SHAPE_DIAGNOSTIC.md).
That single replay is not qualification evidence and must complete before
deciding whether the unchanged formal two-run gate should be rerun.

## Registered configuration

| Item | Registered value |
|---|---|
| Replays | 2, sequential and real-time |
| ASR image | `nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0` |
| ASR repository digest | `sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850` |
| ASR profile selector hash | `8d3fa26a44c471552b7edac76372f3db66e5c1bd73e24abac701091717026c58` |
| Language | `en-US` |
| EOU | 800 ms |
| Word offsets requested | Yes |
| Maximum permitted chunk lateness | 250 ms |

The runtime attestation stayed verified through both runs. It bound the
literal loopback endpoint and Docker port, healthy container state, local image
ID, immutable repository digest, and registered English streaming profile.

## Input identity

| Item | Observed value |
|---|---|
| Source WAV SHA-256 | `2e6b394e7a68cd5d8c39bb332aeff0c790ba059fd4501c3a71a2635181e0f20a` |
| Source samples | 30,209,672 |
| Padded wire samples | 30,211,200 |
| Padded PCM SHA-256 | `9ad08fe1e83e714c48dde4f971606431302fae9d3079293b73ab8ff99d1d8143` |
| Preparation | Strict RIFF PCM16LE passthrough plus final zero padding |
| Audio sent per run | 1,888.2 seconds |

Both runs used the same exact padded wire image.

## Per-run results

| Metric | Run 1 | Run 2 |
|---|---:|---:|
| Input completed | Yes | Yes |
| Wall time | 1,888.459 s | 1,888.457 s |
| Maximum chunk lateness | 1.006 ms | 9.777 ms |
| Mean chunk lateness | 0.451 ms | 0.499 ms |
| Nonempty finals | 437 | 437 |
| Complete word envelopes | 424 | 424 |
| Finals missing a complete envelope | 13 | 13 |
| Complete-envelope rate | 97.025% | 97.025% |
| Gate result | Fail | Fail |

All privacy-safe final metadata other than the local receipt clock was
identical between the runs.

## Repeated attribution failures

The following final IDs were missing a complete word-derived source envelope
in both runs:

```text
98, 179, 214, 215, 237, 261, 274, 295, 296, 302, 315, 376, 404
```

The timing-shape breakdown was identical:

- 10 finals contained one or more word entries and a usable first-word start,
  but did not provide a usable positive final word envelope.
- 3 finals (`261`, `315`, and `376`) contained nonempty text but no word
  entries, leaving only an `audio_processed` end horizon.
- No final was classified as wholly unavailable.

The 13 failures represent 2.975% of nonempty finals. Their exact repeatability
indicates a deterministic response-shape issue for this input/runtime
combination rather than VM scheduling noise or a transient client race.

## Evidence artifact

The full transcript-free report remains ignored from Git:

```text
experiment_results/asr-final-attribution-gate-20260725.json
```

Artifact SHA-256:

```text
ee4661f59a2d1839c36ac10bec2d8aa60de962582571a82ae381394391fa9f6e
```

The report contains final IDs, character counts, timing shapes, hashes,
pacing, and runtime attestation. It contains no transcript, translation,
arbitrary filename, endpoint string, or local container name.

## Decision and next actions

1. Do not run or interpret a formal semantic source-event browser gate from
   this ASR qualification. Exact source attribution is still incomplete.
2. Provide the sanitized counts, repeated final IDs, model version/digest,
   profile hash, EOU, and report hash to the ASR service team. Ask whether
   every nonempty final is expected to carry a complete positive-duration word-time
   envelope when word offsets are requested.
3. Run exactly one privacy-safe word-timing-shape diagnostic. It distinguishes
   absent, unparseable, nonfinite, negative, zero-length, reversed, and valid
   word entries without retaining token or transcript text. Proto3 scalar
   zero is recorded with unobservable presence; it is not labeled missing.
   Review that result before deciding whether another formal two-run
   qualification is justified.
4. Do not silently synthesize a missing source start from neighboring finals.
   Any conservative fallback must be separately specified, reviewed, and
   preregistered. A word-derived start plus an `audio_processed` end could
   potentially cover the 10 incomplete cases, but it does not solve the three
   finals with no word entries.
5. Keep the ASR evidence problem separate from the audience-freshness result.
   The prior diagnostic browser run already exceeded the desired 5–10 second
   bounded queue despite 1.10x no-drop playback. Fixing attribution enables a
   formal semantic measurement; it does not by itself solve accumulated
   audience delay.

The diagnostic command and schema are documented in
[ASR Word-Timing-Shape Diagnostic](ASR_WORD_TIMING_SHAPE_DIAGNOSTIC.md).
Do not substitute its one run for either replay required below.

## Reproduction command

Run from the reviewed merged branch with the configured environment loaded:

```bash
set -a
source .env
set +a
export RIVA_ASR_WORD_TIMES=1
export RIVA_SOURCE_LANGUAGE=en-US
export RIVA_EOU_MS=800

python3 asr_final_attribution_gate.py \
  --file test_audio/long-form-03-30min.wav \
  --uri 127.0.0.1:50052 \
  --docker-container <local-asr-container> \
  --runs 2 \
  --progress-seconds 60 \
  --json-output \
    experiment_results/asr-final-attribution-gate-20260725.json
```
