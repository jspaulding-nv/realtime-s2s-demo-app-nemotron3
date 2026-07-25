# Semantic source-event latency gate

## Status and claim boundary

This is the first post-merge engineering gate for the live-audience question:
how far behind the room could a translated listener be at a known source
moment?

The gate records a human-reviewed source-event coordinate and cryptographically
binds it to the exact padded Int16 PCM wire image. It then uses audio metadata
protocol v1 to find the fully attributed translated parent or identical-range
sibling group. It reports a conservative interval from that source sample to:

- the first and last candidate PCM receipt;
- the final candidate parent-completion receipt;
- the first projected browser playback start; and
- the final projected browser playback end.

It does **not** identify the exact Spanish word or punchline within that audio,
observe Web Audio rendering, or prove physical audibility. A passing report is
a source-event-to-parent-envelope engineering pass, not an audience-latency
claim.

## Why protocol v1 is sufficient

No wire-protocol change is required. With ASR word timing enabled, the existing
strict protocol already carries:

```text
(stream generation, parent ID, frame ID, source start, source end)
```

The Test Dashboard CSV also records, in the same client-monotonic clock:

```text
input PCM sample zero
binary receipt
parent completion
projected browser playback start
scheduled frame duration
```

Adding an event ID to protocol v1 would break its exact-field contract without
creating authoritative target-language alignment. The source marker therefore
stays in a private, capture-bound sidecar and is joined offline.

## Accurate real-time pacing prerequisite

Formal captures must use the chunk-end pacing introduced with this gate:

```text
S0 = client-monotonic input PCM sample-zero time
chunk deadline = S0 + sourceSampleEnd / sampleRate
```

Each complete 300 ms PCM chunk is released no earlier than its end boundary.
Late browser or CLI timers remain late and contribute to the measured delay;
they do not shift `S0` or the remaining absolute deadlines.

At file load, the browser constructs the complete padded Int16 PCM buffer once,
computes SHA-256 over those exact bytes, and streams every chunk from that same
buffer. Every `chunk_sent` CSV row repeats the lowercase digest and total padded
sample count. The analyzer reconciles every row, the final ledger sample, and
the sidecar before setting `source_pcm_binding_verified=true`.
The dashboard prefers native Web Crypto and uses the same tested, block-wise
SHA-256 implementation when a plain-HTTP remote origin does not expose
`crypto.subtle`.

This removes a look-ahead bias in earlier file tests, which released a complete
chunk at its start boundary. The analyzer independently checks every CSV input
ledger row and rejects older or corrupted start-boundary captures.

## Runtime prerequisites

Use the staged schema-3 incremental path:

```dotenv
S2S_PIPELINE_MODE=staged
STAGED_TTS_INCREMENTAL_PUBLISH=1
STAGED_TTS_SUBSEGMENT_MAX_CHARS=0
RIVA_ASR_WORD_TIMES=1
RIVA_EOU_MS=800
```

Then restart the FastAPI backend and verify:

```bash
curl --fail --silent http://localhost:8000/api/config | python3 -m json.tool
```

The response must advertise audio metadata protocol version 1, staged mode,
incremental TTS, and ASR word timing. End-only `audio_processed` offsets are
non-semantic and cannot pass this gate.

## Capture and marker sidecar

Open the Test Dashboard at:

```text
http://localhost:5173/#/test
```

When the application is on a remote machine, forward port 5173 over SSH and
open that localhost URL in the local browser. Select the exact source file and
the intended playback policy, start one test, do not stop it manually, and wait
for the dashboard to reach `Completed` after server output and browser playback
have drained. Select **Export CSV** only then. A failed, interrupted, or
manually stopped run is not formal gate evidence.

Keep the exported CSV and private reviewer material in an ignored directory
such as:

```text
experiment_results/semantic-event-gate/
```

Obtain the exact CSV digest:

```bash
sha256sum experiment_results/semantic-event-gate/timing-export.csv
```

Read the stable `input_pcm_sha256` and `input_pcm_sample_count` values from any
`chunk_sent` row. Confirm that all `chunk_sent` rows contain the same values;
the analyzer repeats this check and also requires the final sample ledger end
to equal the padded sample count.

Create a UTF-8 JSON sidecar with exact keys:

```json
{
  "schema_version": 1,
  "capture_csv_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "source_sample_rate_hz": 16000,
  "source_pcm_sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
  "source_pcm_sample_count": 2880000,
  "markers": [
    {
      "event_id": "event-001",
      "source_sample_index": 960000,
      "independent_reviewer_count": 2
    }
  ]
}
```

Requirements:

- event IDs must use the anonymous `event-NNN` form;
- the sample index identifies the reviewed semantic source moment;
- at least two independent reviewers must agree before the default formal run;
- no transcript, translation, filename, path, organization, person, free-text
  note, endpoint, session ID, or wall-clock timestamp belongs in the sidecar;
- exact duplicate source sample indices are rejected as annotation errors;
- `capture_csv_sha256` must match the CSV bytes exactly;
- `source_pcm_sha256` and `source_pcm_sample_count` must match every input
  ledger row and its final padded sample.

Use this independent annotation procedure:

1. Freeze the capture CSV and its digest before review. Give every reviewer the
   same source audio and the same definition of the semantic instant to mark,
   but do not show them another reviewer's cursor, timestamp, or sample.
2. Each reviewer listens and records a zero-based offset independently. A
   sample-level editor configured for 16,000 Hz may provide the sample directly.
   For a time offset, use:

   ```text
   source_sample_index = floor(source_offset_seconds * 16000)
   ```

   Equivalently, for an exact millisecond offset, use
   `floor(source_offset_milliseconds * 16)`. Sample zero is the first decoded
   sample; the index must be less than `source_pcm_sample_count`.
3. Lock the private individual records before comparing them. If the sample
   indices differ, the reviewers may jointly replay the boundary and reconcile
   one canonical sample, while retaining their initial records privately. If
   they cannot agree on the same semantic instant and final sample, omit that
   marker rather than counting it.
4. Set `independent_reviewer_count` to the number who performed the blind first
   pass and then agreed to the reconciled sample. Store only the anonymous final
   marker in the capture-bound sidecar.

The analyzer verifies that the asserted reviewer count meets the configured
minimum, that the sample is in range, and that the sidecar is bound to the exact
CSV and transmitted PCM identity. It cannot verify that the people worked
independently or that they selected the semantically correct instant; those are
human review controls and must be preserved in the private test record.

Raw annotations and captures remain private/ignored. The generated report
contains only hashes, numeric identity/timing, booleans, counts, and anonymous
event IDs.

Distinct source markers may resolve to the same ASR parent or identical-range
sibling group when several reviewed moments occur within one final. Those
events legitimately share a candidate translated-audio envelope; they are not
independent parent-level observations. Reports assign deterministic anonymous
`group-NNN` identifiers and separately show marker count and unique candidate
group count. Event/status counts are marker counts, not independent trials.

## Run the analyzer

Use an explicit SLA; the command has no implicit audience threshold:

```bash
python3 analyze_semantic_event_latency.py \
  --results-csv \
    experiment_results/semantic-event-gate/timing-export.csv \
  --markers-json \
    experiment_results/semantic-event-gate/source-markers.json \
  --max-latency-seconds 10 \
  --json-output \
    experiment_results/semantic-event-gate/semantic-event-latency.json \
  --markdown-output \
    experiment_results/semantic-event-gate/semantic-event-latency.md
```

Exit codes are suitable for automation:

- `0`: aggregate `PASS`;
- `1`: aggregate `FAIL`;
- `3`: aggregate `INCONCLUSIVE`;
- `2`: invalid arguments, rejected evidence, or report-output I/O failure.

With no output-file options, stdout remains machine-readable JSON. With output
files, stdout is a concise status/format summary and never includes evidence
paths. Requested reports are fully staged before installation; an expected
multi-output installation failure rolls back the set instead of leaving one
new report alongside one missing report.

The current working objective is 5 seconds with 10 seconds as a soft ceiling.
The explicit 10-second example evaluates that ceiling; it does not assert that
10 seconds has been validated with listeners.

## Fail-closed evidence rules

The analyzer rejects the complete gate instead of omitting bad markers when it
finds any of the following:

- CSV/sidecar SHA mismatch, PCM digest/count mismatch, or unknown sidecar
  fields;
- invalid, fragmented, early, or inconsistent input sample ledger;
- a final input-ledger sample that does not equal the padded PCM sample count;
- protocol other than v1 or a stream-generation change;
- missing or mismatched receipt, schedule, parent-completion, frame, byte, or
  source-range evidence;
- end-only or otherwise non-attributed source timing;
- noncontiguous parent/frame identity;
- overlapping or backward-moving distinct source ranges;
- a marker outside transmitted input or with no attributed range;
- duplicate source samples or insufficient reviewers.

Multiple parents are accepted only when they carry the exact same attributed
source range, as can occur when one ASR final is split into ordered punctuation
segments. Their union becomes the conservative candidate envelope.

## Decision rule

For each event:

```text
PASS
  projected final candidate-frame end <= SLA

FAIL
  projected first candidate-frame start > SLA

INCONCLUSIVE
  candidate envelope straddles the SLA
```

The aggregate is `FAIL` if any event fails, otherwise `INCONCLUSIVE` if any
event is inconclusive, otherwise `PASS`.

This deliberately avoids claiming knowledge of the precise Spanish landmark.
If the entire candidate envelope ends before the SLA, the unknown target
landmark must also be before it. If the entire envelope begins after the SLA,
the landmark cannot meet it. A straddling envelope cannot decide the question.

The claim boundary is explicit: `semantic_source_marker_reviewed=true` means
the anonymous coordinate met the reviewer-count requirement, and
`source_pcm_binding_verified=true` means that coordinate's sidecar matched the
exact transmitted padded PCM identity. Neither flag proves the corresponding
target-language landmark or physical audibility; those remain false.

## Promotion beyond the parent envelope

The next evidence tiers require a reviewer-identified target-language landmark:

1. **Scheduled target-sample tier:** retain ignored translated PCM, identify
   the Spanish landmark sample, map it to its exact frame and adaptive playback
   rate, and calculate its projected scheduled time.
2. **Rendered digital tier:** record source reference and post-queue/post-rate
   output on one AudioContext sample clock.
3. **Physical tier:** record English reference and Spanish device output on two
   channels of one external recorder clock.

Only the physical tier may set `actual_audibility_proven=true`. Native-speaker
semantic correctness and 1.05x/1.10x intelligibility review remain separate
required gates.
