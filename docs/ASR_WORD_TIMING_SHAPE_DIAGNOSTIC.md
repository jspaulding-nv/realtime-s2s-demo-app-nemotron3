# ASR Word-Timing-Shape Diagnostic

## Purpose

This diagnostic records the raw *shape* of the ASR word timings returned by
one full real-time replay of the tracked long-form input. It is designed to
answer why a small, repeatable set of nonempty ASR finals did not have a
complete positive-duration first-word-start to last-word-end envelope in the formal
two-run attribution qualification.

The replay is intentionally diagnostic-only. It does not qualify ASR source
attribution, change the formal gate result, measure transcription accuracy, or
measure end-to-end audience latency.

## Why zero has unobservable presence

The installed Riva client represents `WordInfo.start_time` and
`WordInfo.end_time` as singular, non-optional proto3 scalar fields. Their field
descriptors do not support presence. After protobuf decoding, a numeric value
of zero therefore cannot distinguish among:

- an omitted field that decoded to its default;
- a producer that explicitly assigned the default; or
- a genuine boundary at 0 ms.

The diagnostic records that case as:

```json
{
  "presence": "unobservable",
  "numeric_class": "zero",
  "finite_value_ms": 0.0
}
```

`unobservable` describes field presence, not the numeric value. The report
must not relabel this case as `missing`, `unset`, or an ASR service defect
without service-contract evidence.

For a non-protobuf test object, or for a future field that does support
presence, the same schema can record `present` or `absent`.

## Diagnostic schema

The outer report uses schema version 1 and the artifact kind
`asr_word_timing_shape_diagnostic`. Its qualification-related fields are fixed:

```text
diagnostic_only       = true
qualification_eligible = false
qualification_status = "not_evaluated"
requested_run_count   = 1
```

The report deliberately contains no `gate` or `passed` key. A
`completion_status` of `complete` means only that the one-run diagnostic had
exact pre/capture/post source-WAV identity, exact padded-PCM binding, real-time
pacing, full input consumption, word-timing diagnostics for every nonempty
final, and identical verified runtime attestations before and after capture.
The runtime comparison includes a privacy-safe fingerprint of the Docker
container ID, start time, and restart count.

Each run's `attribution` object uses schema version 2. It retains the existing
privacy-safe final-attribution fields and adds a
`word_timing_shape_diagnostics` object to every nonempty final.

### Boundary categories

Each start and end boundary has exactly one presence category:

```text
present
absent
unobservable
```

It also has exactly one numeric category:

```text
not_available
unparseable
nonfinite
negative
zero
positive
```

Finite negative, zero, and positive observations retain their numeric value in
milliseconds. The other numeric categories do not retain a value.

### Relation and shape categories

Every word entry records one numeric relation:

```text
end_after_start
equal
end_before_start
not_comparable
```

It also records exactly one mutually exclusive shape:

```text
valid
absent_boundary
unparseable_boundary
nonfinite_boundary
negative_boundary
zero_length
reversed
```

Classification uses that precedence order after `valid` is excluded:
unavailable boundary, unparseable boundary, nonfinite boundary, negative
boundary, equal boundary, then end-before-start. Any remaining finite
end-after-start pair is `valid`.

The report applies the same relation and shape categories to the result
envelope formed from the first word's start and the last word's end. The
envelope is usable only when its shape is `valid`. A final without word entries
has a null envelope and contributes to the aggregate
`no_word_entries` category.

### Per-final and aggregate records

For each nonempty final, the diagnostic records:

- an anonymous sequential final ID and transcript character count;
- the existing word count, ASR processing horizon, receipt clock, source
  boundaries, and timing basis;
- the first-start/last-end envelope observations and shape;
- fixed counters covering every word entry by shape;
- fixed start and end counters by numeric class and presence; and
- only the non-valid word entries, identified by zero-based word index and
  their boundary observations, relation, and shape.

The aggregate includes:

- timing-basis counts and IDs without a complete word envelope;
- diagnostic-availability counts and IDs;
- finals with no word entries;
- envelope counts and final IDs for `no_word_entries` plus every shape above;
- word-entry shape counts;
- start/end numeric-class and presence counts; and
- the total number of anomalous word entries.

Every fixed counter map includes zero-valued categories. Each per-final
counter group must sum to that final's word-entry count, and every non-valid
entry must appear once in its anomaly list.

## Privacy boundary

The retained artifact contains no:

- transcript, translation, token, or word text;
- speaker, organization, partner, customer, or congregation identity;
- arbitrary input filename or filesystem path;
- endpoint string or local container name;
- image tag or model-profile string; or
- source or translated audio.

It retains hashes and sample counts for exact input binding, anonymous final
IDs, character and word counts, timing values and categories, port numbers,
runtime image identity/digest, and the registered profile-selector hash.
It also retains a one-way container-instance fingerprint so a restart or
replacement during capture invalidates the result without exposing the local
container name.
`experiment_results/` remains ignored by Git; do not commit the raw JSON.

## Distinction from formal qualification

The formal ASR final-attribution gate requires two complete, independent
real-time replays of the same exact padded PCM and zero nonempty finals without
a complete positive-duration word-derived envelope. The prior two-run result
remains failed.

This diagnostic performs exactly one replay so the response shapes can be
understood before another expensive qualification attempt. Its report cannot
be converted into, combined with, or cited as a passing formal gate. The
formal acceptance criterion remains unchanged: two complete runs and zero
incomplete word envelopes. Its evidence implementation is hardened in this
change with computed PCM binding, registered client configuration,
container-instance continuity, and concurrent-attempt ownership.

## Prerequisites

- Use the reviewed branch containing this diagnostic.
- Keep the tracked `test_audio/long-form-03-30min.wav` unchanged.
- Start the pinned ASR service from this repository's Compose configuration
  and wait until `s2s-eval-nemotron-asr` is healthy.
- Use the literal local ASR endpoint `127.0.0.1:50052`; runtime attestation
  verifies its Docker binding, health, immutable image identity, repository
  digest, registered English streaming selector, and container-instance
  continuity.
- Install the pinned Python dependencies, including the Riva client.
- Load `.env` into the shell and enable ASR word-time offsets. Docker Compose
  reads `.env` automatically, but this Python process does not.
- Allow about 32 minutes of uninterrupted VM and service availability.

## Reproduction command

Run from the repository root:

```bash
set -a
source .env
set +a
export RIVA_ASR_WORD_TIMES=1
export RIVA_SOURCE_LANGUAGE=en-US
export RIVA_EOU_MS=800

PYTHONPATH=.python-packages:backend:. \
python3 asr_word_timing_shape_diagnostic.py \
  --file test_audio/long-form-03-30min.wav \
  --uri 127.0.0.1:50052 \
  --docker-container s2s-eval-nemotron-asr \
  --progress-seconds 60 \
  --json-output \
    experiment_results/asr-word-timing-shape-diagnostic-20260725.json
```

Expected local artifact path:

```text
experiment_results/asr-word-timing-shape-diagnostic-20260725.json
```

A completed registered replay and its transcript-free findings are documented
in
[ASR Word-Timing-Shape Diagnostic Result — 2026-07-25](ASR_WORD_TIMING_SHAPE_RESULT_2026-07-25.md).

## Interruption and stale-artifact behavior

Before validating the input, inspecting Docker, or starting the long replay,
the runner replaces any prior diagnostic at the requested path with a new
failed attempt marked `initializing_or_interrupted`. It then checkpoints the
pre-attestation state, the pending capture, and the completed capture awaiting
post-attestation. A VM interruption therefore cannot leave an older completed
diagnostic looking current.

The runner refuses to overwrite the input WAV or an existing formal
qualification artifact. It writes a final `complete` report only after the
post-capture attestation matches the verified pre-capture identity. Attestation
failure, changed runtime identity, changed source-WAV identity, incomplete
input, invalid pacing, or absent diagnostic metadata leaves a failed diagnostic
and a nonzero exit status. The source hash captured from the exact bytes opened
for streaming must match hashes taken before and after the replay.

## Interpretation and next decision

Start with:

```text
runs[0].attribution.envelope_shape_counts
runs[0].attribution.envelope_shape_final_ids
runs[0].attribution.word_entry_shape_counts
runs[0].attribution.start_numeric_class_counts
runs[0].attribution.end_numeric_class_counts
runs[0].attribution.start_presence_counts
runs[0].attribution.end_presence_counts
```

Then inspect the transcript-free per-final diagnostics for the previously
incomplete final IDs. A zero boundary with `presence=unobservable` is evidence
of a decoded zero only; it is not evidence that the field was absent.

Use the result to choose one reviewed next action:

1. If the repeated finals contain zero/unobservable last boundaries or no word
   entries, provide the sanitized shape counts, anonymous IDs, pinned runtime
   identity, and artifact hash to the ASR service team for contract guidance.
2. If the diagnostic exposes a client-side parsing or classification defect,
   correct and test that defect without manufacturing source boundaries.
3. If a conservative fallback is proposed, specify and preregister it
   separately. Do not silently borrow neighboring offsets or treat an
   `audio_processed` horizon as a semantic start.
4. Only after the response contract or remediation is understood, rerun the
   unchanged formal two-run qualification. A passing attribution gate enables
   the later semantic audience-latency measurement; it does not itself prove a
   5–10 second audience-delay target.

The registered July 25 replay found ten zero-duration final envelopes and
three finals with no word entries. It found no absent, unparseable, nonfinite,
negative, numerically zero, or reversed supplied boundaries. Do not repeat the
unchanged two-run gate until the ASR response contract or a supported
remediation is understood.
