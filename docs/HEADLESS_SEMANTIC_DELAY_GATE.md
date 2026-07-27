# Headless scheduled semantic-delay gate

## Purpose and status

This gate directly measures when a reviewed target-language semantic landmark
would be played by the browser-independent listener schedule relative to its
reviewed source-language landmark. It is intended to answer questions such as
whether a listener would hear a short reaction or punchline-like moment within
the current five-second objective and ten-second ceiling.

The gate uses the same real-time-paced WebSocket service path and deterministic
no-drop 5/8/10-second playback policy as the primary
[headless real-time gate](HEADLESS_REALTIME_GATE.md). It does not require
Chrome, Vite, Web Audio, a display server, or an audio device.

Private PCM retention is **off by default**. It is enabled only when
`batch_latency_test.py` receives an explicit
`--private-semantic-capture-dir` argument. Do not enable it for routine
latency runs.

## Claim boundary

A valid result establishes a **reviewed source sample to scheduled translated
sample delay**:

```text
reviewed source sample
  -> real-time ASR, NMT, and TTS processing
  -> translated PCM receipt
  -> deterministic listener queue and playback-rate policy
  -> reviewed translated sample's scheduled time
```

This is stronger than queue depth or a source-parent envelope because two
independent bilingual reviewers identify the corresponding landmark in both
the source and translated PCM. It is still scheduled digital evidence, not
proof that a DAC, loudspeaker, room, or person rendered or heard that sample.

The gate does not establish:

- physical or acoustic audibility;
- synchronization with an in-room audience reaction;
- translation, pronunciation, or voice quality beyond the reviewed landmarks;
- acceptability of accelerated speech for a full session; or
- a latency guarantee for source material, hardware, models, or network paths
  not represented by the capture.

Use a same-clock digital loopback or two-channel external recording when
physical output or audience-reaction alignment is the claim. See
[Audience latency metrics](AUDIENCE_LATENCY_METRICS.md).

## Prerequisites

Use the staged Nemotron 3 ASR, NMT, and Magpie TTS path with:

```dotenv
S2S_PIPELINE_MODE=staged
RIVA_EOU_MS=800
RIVA_ASR_WORD_TIMES=1
STAGED_TTS_INCREMENTAL_PUBLISH=1
STAGED_TTS_SUBSEGMENT_MAX_CHARS=0
```

Restart FastAPI after changing `.env`, then verify its advertised
configuration and the three NIM readiness endpoints as described in
[Headless real-time gate](HEADLESS_REALTIME_GATE.md). The capture requires
audio metadata protocol v1 and staged telemetry schema 3. Do not treat
end-only `audio_processed` offsets as semantic ASR attribution.

Before handling any audio, confirm that:

- the source may be processed and reviewed for this purpose;
- access to the source and generated voice is authorized;
- the reviewers are authorized to hear the material;
- the private storage location meets the applicable retention and data-handling
  requirements; and
- the capture will not be copied into a repository, ticket, chat, or public
  artifact store.

## Private capture contract

Set a restrictive process mask and choose a new ignored directory for each
attempt:

```bash
umask 077
CAPTURE_DIR="experiment_results/semantic-delay-60s-attempt-01"
git check-ignore -q "$CAPTURE_DIR"
```

The last command must exit successfully. `experiment_results/` is ignored by
the repository. Do not use a tracked path, a shared temporary directory, or a
directory synchronized to an unapproved service.

Run the one-minute capture:

```bash
python3 batch_latency_test.py \
  --file test_audio/preflight.wav \
  --backend http://localhost:8000 \
  --audio-metadata-protocol-v1 \
  --private-semantic-capture-dir "$CAPTURE_DIR"
```

`--private-semantic-capture-dir` is the complete opt-in contract. Without that
argument, the harness must not retain source or translated PCM. Use a new
directory rather than mixing attempts or overwriting prior evidence.

The capture directory is mode `0700`; every artifact is mode `0600`. The
runner creates:

```text
<capture-dir>/
├── source-review.wav
├── translated-review.wav
└── schedule-ledger.json
```

Reviewers later add:

```text
<capture-dir>/
└── reviewer-markers.json
```

The files have these roles:

- `source-review.wav` contains the exact source PCM stream used by the
  real-time-paced client, represented as a reviewable WAV.
- `translated-review.wav` contains validated translated PCM in protocol frame
  order. It is a review medium; playing this WAV by itself does not reproduce
  queue waits or adaptive playback rates.
- `schedule-ledger.json` is one JSON object that binds the source and
  translated PCM hashes and sample counts to validated per-frame transport and
  schedule evidence. Its frame records map translated sample ranges to
  parent/frame identity, attributed source ranges, receipt order, deterministic
  scheduled start/end, and playback rate.
- `reviewer-markers.json` binds anonymous, independently reviewed source and
  translated sample indices to that exact capture.

Do not concatenate, normalize, resample, trim, or edit either review WAV after
capture. Do not hand-edit the schedule ledger. Any byte-level change breaks
the evidence binding and requires a new capture or sidecar.

Before review, verify permissions and freeze hashes:

```bash
find "$CAPTURE_DIR" -maxdepth 1 -type f -printf '%m %f\n'
sha256sum \
  "$CAPTURE_DIR/source-review.wav" \
  "$CAPTURE_DIR/translated-review.wav" \
  "$CAPTURE_DIR/schedule-ledger.json"
```

The permissions listing must show `600` for each file. The analyzer
independently verifies the WAV/ledger hashes, sample counts, PCM format,
translated-frame continuity, schedule mapping, and marker ranges.

## Independent bilingual review

Select several anonymous semantic moments, including short reaction or
punchline-like landmarks near the beginning, middle, and end. Define the
landmark rule before review—for example, the onset of the source word that
completes the idea and the onset of the corresponding translated word. Do not
change the rule after seeing the measured delay.

At least two bilingual reviewers must independently mark the source landmark,
and at least two must independently mark its corresponding translated
landmark. Use anonymous reviewer codes in private working records. Do not put
names, email addresses, organizations, transcripts, translations, source
titles, free-text notes, paths, endpoints, session identifiers, or wall-clock
times in the final sidecar.

Use this procedure:

1. Freeze the three runner-generated files and their hashes before review.
2. Give each reviewer the same two WAVs, event definition, and sample-index
   convention without showing another reviewer's markers.
3. Have each reviewer complete and lock a blind first pass for both source and
   translated landmarks.
4. Compare results only after all first passes are locked. Reviewers may then
   replay a boundary and reconcile one canonical sample index.
5. Omit an event if the reviewers cannot agree that the source and translated
   landmarks express the same semantic moment or cannot reconcile a canonical
   boundary. Do not average unrelated or ambiguous landmarks.
6. Record separately how many reviewers independently completed and accepted
   the source and translated boundaries.

Sample indices are zero-based positions in the PCM carried by each review WAV.
When an editor reports seconds:

```text
sample_index = floor(offset_seconds * WAV_sample_rate_hz)
```

Sample zero is the first PCM sample, and every index must be smaller than its
corresponding PCM sample count. Use the sample rate declared by each WAV; do
not assume the source and translated files have different or identical rates.

## Reviewer sidecar schema

Create `reviewer-markers.json` as UTF-8 JSON with exactly these top-level and
event keys. The top-level event array is named `events`:

```json
{
  "schema_version": 1,
  "schedule_ledger_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "source_pcm_sha256": "123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0",
  "source_pcm_sample_count": 960000,
  "translated_pcm_sha256": "23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01",
  "translated_pcm_sample_count": 1040000,
  "events": [
    {
      "event_id": "event-001",
      "source_sample_index": 160000,
      "translated_sample_index": 248000,
      "source_independent_reviewer_count": 2,
      "translated_independent_reviewer_count": 2
    }
  ]
}
```

Copy the lowercase SHA-256 values and exact PCM sample counts from the frozen
capture evidence; the example values above are placeholders. Requirements:

- `schema_version` is `1`;
- each `event_id` uses the anonymous `event-NNN` form and is unique;
- both sample indices are plain, non-negative integers within their respective
  PCM streams;
- both independent reviewer counts are plain integers of at least `2`;
- source and translated sample indices identify the reconciled semantic
  landmarks, not convenient frame boundaries;
- duplicate event IDs, duplicate source sample indices, or duplicate
  translated sample indices are annotation errors; and
- the object contains no extra identity, content, path, or free-text fields.

The counts assert that independent review occurred; software cannot prove that
the reviewers worked independently or selected semantically correct moments.
Retain the locked first-pass records privately under the governing retention
policy, but do not add identities or raw notes to the sidecar.

After creating the sidecar:

```bash
chmod 600 "$CAPTURE_DIR/reviewer-markers.json"
```

## Analyze the five-second objective

Run the analyzer against all four hash-bound inputs:

```bash
python3 analyze_scheduled_semantic_delay.py \
  --ledger "$CAPTURE_DIR/schedule-ledger.json" \
  --source-wav "$CAPTURE_DIR/source-review.wav" \
  --translated-wav "$CAPTURE_DIR/translated-review.wav" \
  --markers "$CAPTURE_DIR/reviewer-markers.json" \
  --max-latency-seconds 5 \
  --json-output "$CAPTURE_DIR/semantic-delay-5s.json" \
  --markdown-output "$CAPTURE_DIR/semantic-delay-5s.md"
```

The analyzer maps each reviewed translated sample through its exact protocol
frame, translated-sample offset, scheduled start, and playback rate. It
subtracts the reviewed source sample's real-time input coordinate to obtain
the scheduled semantic delay. A threshold result is valid only after all
hashes, sample counts, PCM continuity, schedule entries, and reviewer
requirements pass fail-closed validation.

The mapped target frame's completed parent must also carry a complete ASR
source range containing the reviewed source marker. An end-only fallback or a
neighboring parent's range cannot supply semantic attribution. ASR range
start/end offsets use closed containment; source and translated PCM samples
use half-open one-sample timing intervals.

Treat five seconds as the working objective. Report each event, the aggregate
distribution supported by the marker count, and the maximum. A small marker
set is not enough to make a population-level p95 claim.

## Analyze the ten-second ceiling

Evaluate the same immutable capture and sidecar separately:

```bash
python3 analyze_scheduled_semantic_delay.py \
  --ledger "$CAPTURE_DIR/schedule-ledger.json" \
  --source-wav "$CAPTURE_DIR/source-review.wav" \
  --translated-wav "$CAPTURE_DIR/translated-review.wav" \
  --markers "$CAPTURE_DIR/reviewer-markers.json" \
  --max-latency-seconds 10 \
  --json-output "$CAPTURE_DIR/semantic-delay-10s.json" \
  --markdown-output "$CAPTURE_DIR/semantic-delay-10s.md"
```

Do not relabel a five-second miss as a pass merely because it remains under
ten seconds. Use these interpretations:

- at or below five seconds: meets the current scheduled semantic objective;
- above five and at or below ten seconds: misses the objective but remains
  within the soft ceiling; and
- above ten seconds: misses the live-audience ceiling for that reviewed event.

A gate-invalid result is neither a latency pass nor a latency failure. Correct
the evidence problem and recapture rather than dropping a slow or malformed
event.

## Privacy, sharing, and retention

All four inputs and reviewer-level records are private. The translated WAV can
reveal source meaning, speaker characteristics, model behavior, and evaluation
content even when filenames and JSON fields are anonymous. Hashes bind
evidence; they do not anonymize it.

Apply these controls:

- keep directories mode `0700` and files mode `0600`;
- use an approved encrypted location with least-privilege access;
- do not upload review audio to public or consumer transcription services;
- do not commit or attach WAVs, ledgers, sidecars, reviewer records, raw
  reports, or absolute paths to a pull request or issue;
- keep reviewer codes and identities outside the aggregate artifact;
- follow the shortest approved retention period; and
- manually sanitize aggregate conclusions before sharing them.

Verify that Git still ignores every artifact before any commit:

```bash
git status --short --ignored "$CAPTURE_DIR"
git check-ignore -v "$CAPTURE_DIR/source-review.wav"
git check-ignore -v "$CAPTURE_DIR/translated-review.wav"
git check-ignore -v "$CAPTURE_DIR/schedule-ledger.json"
git check-ignore -v "$CAPTURE_DIR/reviewer-markers.json"
```

Only a reviewed, neutral aggregate conclusion belongs in tracked
documentation. Even analyzer reports remain private by default because hashes
and unusual measurements may correlate to a specific capture.

## Promotion sequence

Promote by evidence tier, not merely because the command completed.

### Phase 1: 60-second control

Use `test_audio/preflight.wav` and at least three unambiguous landmarks spread
across the beginning, middle, and end. Require:

- complete real-time input and terminal output drain;
- no missing, duplicated, reordered, or unmapped translated PCM;
- valid hash/sample/schedule bindings;
- two independent bilingual source and translated reviews per event;
- every event within the ten-second ceiling; and
- a documented distinction between five-second objective passes and misses.

Investigate any ten-second miss before promotion. A five-second miss may
continue only as a diagnostic; it is not an objective pass.

### Phase 2: five-minute run

Use one approved five-minute input and a new private capture directory. Do not
reuse or trim the 60-second artifacts. Add landmarks around pauses, short
reactions, dense clauses, and the beginning, middle, and end. Confirm that
semantic delay and queue depth remain bounded rather than growing with elapsed
source time.

The private-capture option is initially integrated only with
`batch_latency_test.py`. Do not claim that
`run_long_form_experiment.py` accepts or preserves this private evidence.

### Phase 3: long-form runs

Run each long-form input separately through `batch_latency_test.py --file`,
using one new capture directory per input and attempt. Predeclare landmarks
near the beginning, middle, and end and retain slow events. Compare:

- reviewed scheduled semantic delay;
- listener queue depth at each event;
- ASR finalization and NMT/TTS stage timing;
- translated/source duration expansion; and
- terminal listener tail.

Promote the configuration only if the ten-second ceiling remains bounded
without drops and the five-second objective is met often enough for the agreed
evaluation protocol. Any 1.05x or 1.10x playback promotion also requires
separate native-listener intelligibility and naturalness review.

## Cleanup

Stop any process still writing to the capture, preserve only the approved
private evidence or sanitized aggregate, and verify the path before deletion:

```bash
realpath -- "$CAPTURE_DIR"
git check-ignore -q "$CAPTURE_DIR"
find "$CAPTURE_DIR" -maxdepth 1 -type f -printf '%m %f\n'
```

After the retention owner confirms that the capture may be deleted:

```bash
rm -rf -- "$CAPTURE_DIR"
```

Clear `CAPTURE_DIR` from the shell after deletion:

```bash
unset CAPTURE_DIR
```

Deletion from a live filesystem does not guarantee erasure from snapshots,
backups, SSD wear-leveling, or synchronized replicas. Use the approved storage
system's retention and secure-erasure procedure when that distinction matters.
