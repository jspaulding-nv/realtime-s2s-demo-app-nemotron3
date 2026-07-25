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
- the conservative client-clock lower bound for the first candidate browser
  schedule; and
- the conservative client-clock upper bound for the final candidate browser
  schedule.

It does **not** identify the exact Spanish word or punchline within that audio,
observe Web Audio rendering, or prove physical audibility. A passing report is
a source-event-to-parent-envelope engineering pass, not an audience-latency
claim.

The July 25 long-form diagnostic remains **INVALID evidence**, not a semantic
PASS or FAIL. Three ASR finals lacked word-derived source starts, and the
browser clocks crossed the provisional short-run linkage limits after a
discrete offset step. The continuous-clock and ASR-qualification requirements
below apply only to a fresh capture; they do not retroactively validate that
diagnostic. See
[Semantic event gate long-form diagnostic](SEMANTIC_EVENT_GATE_LONG_FORM_DIAGNOSTIC_2026-07-25.md).

## Why protocol v1 is sufficient

No wire-protocol change is required. With ASR word timing enabled, the existing
strict protocol already carries:

```text
(stream generation, parent ID, frame ID, source start, source end)
```

The Test Dashboard CSV also records the client-monotonic timing, Web Audio
schedule, and continuous clock-mapping evidence:

```text
input PCM sample zero
binary receipt
parent completion
projected browser playback start
scheduled AudioContext start and end
continuous output-timestamp or currentTime-bracket samples
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

The registered long-form replacement run must take the deterministic RIFF
passthrough path. The tracked file is already little-endian 16 kHz mono PCM16,
so the browser verifies its declared RIFF length, PCM format, channel count,
sample rate, byte rate, block alignment, bit depth, and unique `fmt`/`data`
chunks. It then copies the `data` payload byte for byte, zero-pads only the
final 4,800-sample wire chunk, and hashes that complete padded wire image.
There is no `decodeAudioData`, resampling, float conversion, or requantization
on this path. The application's general decoder fallback remains available for
other audio, but using that fallback disqualifies this registered formal run.

The binding registered before the replacement run is:

| Input property | Registered value |
| --- | --- |
| Tracked WAV SHA-256 | `2e6b394e7a68cd5d8c39bb332aeff0c790ba059fd4501c3a71a2635181e0f20a` |
| Source / padded samples | 30,209,672 / 30,211,200 |
| PCM preparation basis | `riff_pcm16le_passthrough_zero_pad_v1` |
| Padded wire PCM SHA-256 | `9ad08fe1e83e714c48dde4f971606431302fae9d3079293b73ab8ff99d1d8143` |

The prior padded digest
`b3e622c467fb4be80622df5c2fea708cf08ca49a000cf46933bdadd0e1b9e11b`
was produced by the historical Web Audio decode/resample path. It remains
bound only to the invalid diagnostic capture and is not reusable for the ASR
qualification, replacement browser capture, marker sidecar, or semantic gate.
Even though its padded sample count is the same, its PCM bytes are not.

This removes a look-ahead bias in earlier file tests, which released a complete
chunk at its start boundary. The analyzer independently checks every CSV input
ledger row and rejects older or corrupted start-boundary captures.

## Continuous playback-clock prerequisite

Web Audio schedules PCM on `AudioContext.currentTime`, while the CSV reports
latency on the client `performance.now()` clock. A fresh offset estimate for
every small frame is not a reliable clock mapping because `currentTime`
advances in render quanta while `performance.now()` advances continuously.
Formal captures still retain one ordered playback cursor:

```text
contextStart[i] =
  max(audioContextAtSchedule[i], contextEnd[i-1])

projectedStart[i] =
  max(schedulePerformance[i], projectedEnd[i-1])

projectedEnd[i] =
  projectedStart[i] + scheduledDuration[i] * 1000
```

The first frame has no prior end. A real queue underrun reanchors the projected
cursor at the later scheduling timestamp; frames that remain queued advance
exactly by their rate-adjusted scheduled duration. Start, stop, and restart
reset both cursors. The analyzer independently replays both recurrences across
parent boundaries and rejects event-local re-projection, hidden context gaps,
overlaps, or stale-session state.

The client additionally samples the clock relationship every 200 ms while
audio is queued, with explicit samples at session start, queue start, queue
drain, and session stop. Every disjoint queued interval must have exactly one
unique, ordered start/drain pair; reused, nested, missing, or surplus
boundaries reject the capture. The sequence must be unfragmented, with no
performance-clock or AudioContext gap above 500 ms. The 200 ms cadence is the
collection target; the 500 ms maximum is the fail-closed coverage limit.

Each continuous sample brackets the `currentTime` read:

```text
performanceBefore = performance.now()
context = audioContext.currentTime
outputTimestamp = audioContext.getOutputTimestamp()  # when initialized
performanceAfter = performance.now()
```

Every sample contributes the portable `currentTime` bracket:

```text
offsetLower = performanceBefore - context * 1000
offsetUpper = performanceAfter  - context * 1000
```

A `getOutputTimestamp()` pair contributes an additional mapping point only
when both components are nonfuture relative to `context` and
`performanceAfter`, and neither component is more than 500 ms old:

```text
outputOffset = outputTimestamp.performanceTime
             - outputTimestamp.contextTime * 1000
```

If the method is unavailable, fails, returns its all-zero placeholder, or
returns a future/stale pair, the browser downgrades that observation to the
`current_time_bracket` basis and omits the output-timestamp fields. The
analyzer independently rejects any observation labeled
`get_output_timestamp` whose pair is future or more than 500 ms stale.

For every adjacent sample pair inside one queued interval, monotonicity of both
browser clocks also supplies a cross-corner rectangle:

```text
crossLower[i, i+1] =
  performanceBefore[i] - context[i+1] * 1000

crossUpper[i, i+1] =
  performanceAfter[i+1] - context[i] * 1000
```

These deliberately broad inter-sample bounds contain a transient offset
excursion even if it changes and returns between timer callbacks. The analyzer
forms one capture-wide observed interval from the per-sample brackets, valid
output-timestamp points, per-frame schedule offsets, and every cross-corner
rectangle:

```text
observedLower =
  min(sample lowers, output points, schedule offsets, cross lowers)

observedUpper =
  max(sample uppers, output points, schedule offsets, cross uppers)

guardedLower = observedLower - 32 ms
guardedUpper = observedUpper + 32 ms
```

The 32 ms guard is registered before the replacement run. The previous fixed
25 ms per-frame residual and 50 ms capture-span rejection limits are no longer
the evidence model. Residual and raw-span values remain useful diagnostics,
but formal decisions use the complete guarded interval.

For a candidate parent envelope, the formal client-clock bounds are mapped
from the Web Audio schedule coordinates rather than trusted from the raw
projected client fields:

```text
conservativeStart =
  minimumCandidateScheduledStartContext * 1000 + guardedLower

conservativeEnd =
  maximumCandidateScheduledEndContext * 1000 + guardedUpper
```

A discrete clock step expands the capture-wide interval. It therefore moves
the lower decision bound no later and the upper decision bound no earlier.
Such a step can turn a result into INCONCLUSIVE or invalidate a trace with
insufficient sampling, but it cannot improve a PASS. The maximum 500 ms queued
sampling gap prevents an unobserved long interval from being treated as
continuous evidence.

This remains a browser scheduling projection. It does not observe rendered
device output and does not set `actual_audibility_proven=true`.

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

## Real-time ASR attribution qualification

Before a full browser gate run, replay the exact selected long-form input to
the pinned direct ASR service twice at real-time chunk-end pace:

```bash
python3 asr_final_attribution_gate.py \
  --file test_audio/long-form-03-30min.wav \
  --uri 127.0.0.1:50052 \
  --docker-container <local-asr-container> \
  --runs 2 \
  --json-output \
    experiment_results/semantic-event-gate/asr-final-attribution.json
```

The local container name is used only for Docker inspection and is omitted
from the report. Before streaming, the runner requires the literal
`127.0.0.1` ASR endpoint and verifies a compatible Docker host-port binding;
`localhost` is rejected as ambiguous. It attests that the selected container
is running and healthy, exposes the configured host port at container port
50052, uses the configured pinned image, and resolves to
`nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0` at
`sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850`.
It also binds the running container's local image ID to the inspected image.
The container must contain exactly one
`NIM_TAGS_SELECTOR=name=nemotron-asr-streaming,type=en-US,batch_size=32`
entry, matching the gate's registered selector and SHA-256
`8d3fa26a44c471552b7edac76372f3db66e5c1bd73e24abac701091717026c58`.
The runner repeats the attestation after every complete replay and invalidates
the qualification if any attested identity field changes. The report must set
`asr.runtime_attestation.verified=true`; a configured environment value or a
successful gRPC connection alone is not runtime-identity proof.

When `--json-output` is provided, the runner first replaces any prior artifact
with a durable `passed=false`, zero-run checkpoint carrying a fresh attempt ID
and start time. It checkpoints the attested pre-run state and each completed
run with file and directory synchronization. A process interruption or VM
lease loss therefore cannot leave an older passing report masquerading as the
current attempt.

The command must exit `0` and the ignored JSON report must contain top-level
`passed=true`. Both runs must:

- consume the complete 16 kHz mono 16-bit PCM input;
- release every padded chunk no earlier than its absolute source-end deadline;
- keep maximum observed chunk-release lateness at or below the registered
  250 ms limit, with maximum/mean lateness retained per run;
- produce at least one nonempty final;
- report `attribution.final_missing_word_offsets_count=0`; and
- report `attribution.all_nonempty_finals_have_word_offsets=true`.

The report must also set
`input.exact_padded_pcm_binding_verified=true`, with one identical non-null
padded PCM SHA-256 and sample count across both runs. For the tracked input,
the report must also set
`input.pcm_preparation_basis=riff_pcm16le_passthrough_zero_pad_v1` and match
the registered padded hash
`9ad08fe1e83e714c48dde4f971606431302fae9d3079293b73ab8ff99d1d8143`.
Both the Python runner and browser enforce the same strict RIFF contract:
declared RIFF length, unique valid `fmt ` and `data` chunks, PCM format 1,
mono, 16-bit, 16 kHz, and consistent byte-rate/block-alignment fields.
Use that exact input for the browser capture and confirm its
`input_pcm_sha256` and `input_pcm_sample_count` match the qualification report.
A failed run, a different padded wire image, early/start-boundary pacing, or
even one nonempty final without word offsets blocks the full semantic gate. Do
not infer a missing start from neighboring finals or an `audio_processed` end.

This two-run check qualifies the selected ASR/input/runtime combination for the
next experiment; it is not a general guarantee that every future stream will
contain word offsets. The full analyzer continues to reject end-only source
timing. Its report intentionally contains no arbitrary input filename, endpoint
string, transcript, translation, image tag, or model profile. It binds the
input by hashes/counts, represents endpoint configuration as a boolean, and
records only privacy-safe final lengths, counts, timing, and provenance fields.

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

Nemotron word envelopes plus punctuation splitting can also produce a narrower
later range with the same source end as the preceding range. The trace accepts
only this ordered same-end suffix shape: the later start must strictly advance
and the end must be exactly equal. Crossing, backward, different-end, and all
other containing overlaps remain invalid. A reviewed marker that falls inside
more than one distinct range rejects the complete gate as ambiguous; only
parents with one exact shared range are unioned into a candidate envelope.

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
- broken AudioContext or projected-playback recurrence;
- missing, fragmented, non-monotonic, or invalid browser clock samples;
- a queued playback-clock sampling gap above 500 ms or anything other than one
  unique, ordered start/drain pair per queued interval;
- an invalid `currentTime` bracket or an observation labeled
  `get_output_timestamp` whose pair is future or more than 500 ms stale;
- backward-moving ranges or an unsupported distinct source-range overlap;
- a marker that resolves to more than one distinct attributed range;
- a marker outside transmitted input or with no attributed range;
- duplicate source samples or insufficient reviewers.

Multiple candidate parents are accepted only when they carry the exact same
attributed source range, as can occur when one ASR final is split into ordered
punctuation segments. Their union becomes the conservative candidate envelope.

## Decision rule

For each event:

```text
PASS
  conservative projected final candidate-frame end upper bound <= SLA

FAIL
  conservative projected first candidate-frame start lower bound > SLA

INCONCLUSIVE
  candidate envelope straddles the SLA
```

The aggregate is `FAIL` if any event fails, otherwise `INCONCLUSIVE` if any
event is inconclusive, otherwise `PASS`.

This deliberately avoids claiming knowledge of the precise target-language
landmark. If the entire guarded capture-wide clock-mapped candidate envelope
ends before the SLA, the unknown target landmark must also be before it. If
the entire adjusted envelope begins after the SLA, the landmark cannot meet
it. A straddling envelope cannot decide the question.

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
