# Offline semantic review assistant

## Purpose

This workflow helps independent bilingual reviewers mark the same semantic
moments in source-language and translated speech. It keeps the judgment step
human while making the evidence binding, sample selection, structured
feedback, and final latency analysis repeatable.

The review page is local reviewer tooling only. It is not part of the deployed
speech-to-speech path and does not make a browser a deployment dependency. The
page has no upload step or network dependency; the selected files stay on the
reviewer's device.

The resulting measurements answer a narrow question: when would a reviewed
translated landmark be scheduled relative to the corresponding source
landmark in this capture? They do not prove physical audibility, room
synchronization, general translation quality, or acceptable voice quality for
an entire session.

## Roles and separation

Use one coordinator and at least two bilingual reviewers:

- The coordinator prepares one frozen, hash-bound assignment and gives the
  same five-file packet to every reviewer.
- Each reviewer completes and exports a blind first pass without seeing anyone
  else's observations.
- The coordinator validates and compares the locked observations.
- Reviewers may discuss a boundary only after every first pass is locked. The
  coordinator then records explicit canonical sample indices only when every
  assigned event has sufficient independent, accepted, confident support.

The independence attestation records a reviewer's assertion. Software cannot
prove that the people, devices, or review sessions were independent.

## Private five-file packet

Each reviewer receives exactly these files:

```text
semantic-review-assistant.html
review-assignment.json
schedule-ledger.json
source-review.wav
translated-review.wav
```

The assignment binds the schedule ledger and both PCM streams by SHA-256 and
sample count. The two WAVs are the listening media. The ledger preserves the
validated translated-frame schedule needed by the later latency calculation.
The HTML page verifies these bindings locally before enabling review controls.

Audio can reveal content, speaker characteristics, generated voice behavior,
and evaluation material. Hashes are evidence identifiers, not anonymization.
Treat the entire packet, each reviewer observation, and all coordinator outputs
as private.

Before distribution:

1. Confirm that the source and generated audio may be reviewed for this
   purpose.
2. Confirm that every reviewer is authorized to hear it.
3. Use an approved encrypted transfer and storage location with
   least-privilege access.
4. Keep capture directories mode `0700` and files mode `0600`.
5. Apply the shortest approved retention period.

Do not commit or send the packet, observations, marker sidecars, reports, or
raw hashes through a repository, pull request, issue, ticket, chat, public file
share, or consumer transcription service.

## Coordinator: prepare the packet

Work only in a new ignored private capture directory created by
`batch_latency_test.py --private-semantic-capture-dir`. It must already contain
the three frozen capture files:

```text
schedule-ledger.json
source-review.wav
translated-review.wav
```

Set a restrictive mask and confirm that Git ignores the directory:

```bash
umask 077
CAPTURE_DIR="experiment_results/semantic-delay-review-attempt-01"
git check-ignore -q "$CAPTURE_DIR"
```

For the 60-second control review, generate three windows spread across the
beginning, middle, and end:

```bash
python3 semantic_review_workflow.py prepare \
  --capture-dir "$CAPTURE_DIR" \
  --default-three-windows
```

The command validates the ledger and both WAVs, refuses unsafe permissions or
symbolic links, and creates `review-assignment.json` plus a private copy of
`semantic-review-assistant.html`. It does not alter the frozen capture files.
Generated files are created mode `0600` with no-clobber semantics; an existing
output causes the command to stop rather than silently replace evidence.

For a predeclared protocol, replace `--default-three-windows` with repeated
event windows:

```bash
python3 semantic_review_workflow.py prepare \
  --capture-dir "$CAPTURE_DIR" \
  --event-window event-001:6:18 \
  --event-window event-002:28:40 \
  --event-window event-003:48:58
```

Window coordinates are seconds in the source stream. Use at least three
ordered, non-overlapping windows. Predeclare them before review; do not move a
window to hide a difficult or slow event. For five-minute or long-form audio,
use narrow, explicitly chosen windows instead of the percentage-based default;
otherwise each review window becomes unnecessarily difficult to navigate.
The current page is qualified for the 60-second control. Its canvases remain
whole-stream overviews, so add and validate a zoomed-window review workflow
before asking reviewers to mark precise word onsets in 30–40-minute captures.

Confirm the five files and permissions without printing their contents:

```bash
find "$CAPTURE_DIR" -maxdepth 1 -type f -printf '%m %f\n' | sort
```

Every file must show mode `600`. Give each reviewer a separate encrypted copy
of the same five-file packet.

## Reviewer: complete a blind first pass

No service, command-line tool, development environment, or network connection
is required on the review device. A current browser is used only as a local
audio and annotation interface.

1. Disconnect from unneeded networks if required by the handling policy.
2. Open the packet's `semantic-review-assistant.html` directly in the browser.
3. Select the requested assignment, schedule ledger, source WAV, and
   translated WAV when prompted.
4. Wait until the page reports successful assignment-schema, exact-ledger,
   sample-count, complete-WAV, and PCM-format/hash binding. The coordinator's
   `prepare` step already performed the full ledger and schedule validation;
   the page proves that the reviewer received those exact ledger bytes. Do not
   continue past a warning or mismatch.
5. Read the fixed landmark rule and attest that this is an independent review.
6. For every preselected event, listen within the displayed source window.
   Mark the onset of the source word that completes the idea.
7. Find the word onset in the translated audio that expresses the corresponding
   idea, and mark it. Use the waveform, seek, numeric sample control, and nudge
   controls to refine each boundary.
8. Record semantic equivalence as `accepted`, `uncertain`, or `rejected`.
9. Rate source-boundary confidence, translated-boundary confidence,
   translation quality, intelligibility, and naturalness using the displayed
   1/3/5 anchors; use 2 or 4 only for an intermediate judgment.
10. Select only applicable structured issue flags. Do not encode comments or
    identity in filenames or other fields.
11. Review all entries and export the anonymous observation JSON.
12. Close the page and retain the exported file only in the approved private
    location.

The fixed rule identifier is
`source_idea_completion_word_onset_to_corresponding_translated_word_onset_v1`.
Apply that same rule to every event. The available issue flags are meaning
mismatch (`meaning_mismatch`), omission (`omission`), addition (`addition`),
pronunciation (`pronunciation`), unnatural prosody (`unnatural_prosody`), too
fast (`too_fast`), too slow (`too_slow`), and boundary ambiguous
(`boundary_ambiguous`). They are structured categories, not substitutes for a
transcript or free-text comment.

If an idea is omitted and no corresponding translated word onset exists, mark
the nearest audible candidate or the surrounding phrase where it was expected,
choose `rejected`, add `omission` and (when appropriate)
`boundary_ambiguous`, and set translated-boundary confidence to `1`. This
required sample is diagnostic only and cannot make the event eligible for
reconciliation.

The page generates an anonymous random review-session identifier. It does not
ask for a name, email address, organization, transcript, translation, source
title, or free-text note. Do not add any of those items to the JSON or its
filename. Do not manually edit the exported observation.

Complete and lock the entire first pass before communicating markers,
ratings, or flags to another reviewer. Reviewers must not exchange observation
files until the coordinator confirms that every required blind pass has been
received.

## Coordinator: validate and compare observations

Store returned observations in a private directory, retain their original
bytes, and keep reviewer identity mapping outside the artifacts. Supply at
least two independently exported files:

```bash
install -d -m 700 "$CAPTURE_DIR/private-reviews"
chmod 600 "$CAPTURE_DIR"/private-reviews/review-*.json

python3 semantic_review_workflow.py compare \
  --capture-dir "$CAPTURE_DIR" \
  --review private-reviews/review-01.json \
  --review private-reviews/review-02.json
```

The comparison validates the exact observation schema and every capture hash,
sample count, event, sample bound, independence attestation, rating, and flag.
Its terminal summary uses anonymous labels such as `review-1`; it does not
print review-session identifiers or paths. Treat even this summary as private
because measurements can correlate to a capture.

Relative `--review` paths resolve inside the capture directory. Absolute
review paths are accepted only when they also remain inside that directory.

Compare:

- semantic-equivalence decisions;
- source and translated marker differences;
- boundary confidence;
- translation quality;
- intelligibility;
- naturalness; and
- structured issue flags.

Do not average markers or let software guess which semantic boundary is
correct. An `uncertain` or `rejected` observation is useful evidence, not a
value to coerce into agreement.

## Coordinator: reconcile explicit canonical markers

After the blind observations are locked, reviewers may replay disputed
boundaries and discuss semantic equivalence. Reconcile the assignment only
when every event's source and translated landmarks independently have at least
two reviewers who accepted the semantic match and met that landmark's minimum
boundary confidence. The eligible source and translated subsets may differ;
their counts remain separate in the final marker sidecar.

Choose one explicit source and translated sample index for every event in the
frozen assignment. Each canonical index must stay inside the range marked by
its eligible reviewers. Pass every assigned selection on the command line:

```bash
python3 semantic_review_workflow.py reconcile \
  --capture-dir "$CAPTURE_DIR" \
  --review private-reviews/review-01.json \
  --review private-reviews/review-02.json \
  --canonical event-001:SOURCE_SAMPLE:TRANSLATED_SAMPLE \
  --canonical event-002:SOURCE_SAMPLE:TRANSLATED_SAMPLE \
  --canonical event-003:SOURCE_SAMPLE:TRANSLATED_SAMPLE
```

Replace each uppercase placeholder with the deliberately reconciled,
zero-based integer sample index. Do not use an arithmetic mean, midpoint,
automatically inferred boundary, frame edge, or convenient timestamp. If an
event remains semantically ambiguous or lacks sufficient accepted and
confident independent support, do not manufacture or omit a marker. The
assignment remains unresolved as a whole: reconciliation writes no formal
marker sidecar, and formal analysis remains unavailable/inconclusive until
every assigned event is resolved.

Successful reconciliation writes:

- `reviewer-markers.json`, the exact aggregate marker sidecar consumed by the
  scheduled semantic-delay analyzer; and
- `reviewer-feedback.json`, aggregate structured ratings and issue counts
  without reviewer identities or free text.

Both files remain private, mode `0600`, and hash-bound to the frozen capture.
They do not certify that the reviewers truly worked independently or that the
chosen moments represent all content.

## Coordinator: run the 5-second and 10-second gates

Run both registered thresholds from the same immutable evidence:

```bash
python3 semantic_review_workflow.py analyze \
  --capture-dir "$CAPTURE_DIR"
```

The workflow validates the bundle again and writes
`semantic-delay-5s.json`, `semantic-delay-5s.md`,
`semantic-delay-10s.json`, and `semantic-delay-10s.md`. These private reports
cover the five-second objective and ten-second ceiling. The command returns
the worst gate result rather than treating a ten-second pass as a five-second
pass.

Interpret the event results as follows:

- At or below 5 seconds: meets the current scheduled semantic-delay objective.
- Above 5 and at or below 10 seconds: misses the objective but remains within
  the soft ceiling.
- Above 10 seconds: misses the current live-listener ceiling.
- Invalid or inconclusive: not a latency pass or latency failure; repair the
  evidence or review protocol and rerun without dropping an unfavorable event.

Reports establish scheduled digital delay only for the reviewed landmarks,
capture, model configuration, machine, and network path. They do not establish
physical playback delay, audience-reaction synchronization, population-level
quality, a percentile from three events, or performance on other content.
Native-listener quality review remains separately necessary before promoting
playback acceleration or a voice/model change.

## Troubleshooting

### The page does not enable review controls

- Select the exact four evidence/assignment files from the same packet.
- Wait for all digest and WAV parsing checks to finish.
- Do not rename content inside JSON, edit a WAV, normalize audio, resample,
  trim silence, concatenate files, or regenerate a ledger.
- If any digest, sample count, PCM format, event window, or ledger binding
  differs, discard the mixed packet and request a fresh complete packet.
- If local browser policy blocks file access or cryptographic APIs, use an
  approved current browser on an authorized review device. Do not upload the
  files to work around the policy.

### `compare` rejects an observation

- Confirm that it was exported by the supplied review page after successful
  validation.
- Confirm that the reviewer used the same frozen assignment and evidence.
- Do not repair JSON by hand or add missing ratings, identity, or notes.
- Have the reviewer reopen the original five files and export a new blind
  observation when appropriate.

### `reconcile` rejects a canonical marker

- Confirm that the event has at least the required number of distinct,
  independently attested, `accepted` reviews.
- Confirm that both boundary-confidence ratings meet the assignment minimum.
- Use zero-based integer sample indices inside the eligible reviewers'
  observed range and the corresponding PCM bounds.
- Supply at least three supported events with unique, ordered landmarks.
- If reviewers cannot agree, preserve the disagreement and report the gate as
  inconclusive. Do not average or infer a value.

### A file, permission, or hash check fails

- Stop; do not weaken validation or copy content into an unprotected path.
- Confirm the capture directory is a real ignored directory with mode `0700`
  and that every artifact is a regular, non-symbolic-link file with mode
  `0600`.
- Restore the complete frozen packet from the approved encrypted source or
  create a new capture. Do not overwrite or splice evidence from different
  attempts.

## Retention and cleanup

Keep locked reviewer observations long enough to audit reconciliation under
the approved policy, but keep the reviewer-to-person mapping in a separately
controlled record. Aggregate artifacts deliberately exclude identity and free
text; that does not make them public.

Before any deletion, have the retention owner confirm the correct capture and
approved disposal procedure. Filesystem deletion may not erase snapshots,
backups, synchronized replicas, or SSD remanence. Use the storage system's
approved retention and secure-erasure controls where required.
