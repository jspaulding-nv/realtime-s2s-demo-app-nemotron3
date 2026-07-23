# Feature-flagged staged WebSocket integration

## Status and scope

The bounded direct `ASR -> punctuation splitter -> NMT -> TTS` pipeline is now
available through the existing `/ws/translate` endpoint behind an explicit,
default-off backend feature flag. The client protocol and translated PCM format
are unchanged.

This milestone is an integration and observability gate. Its one-minute live
WebSocket check and first full-length Sample 03 operational canary passed. The
long run also showed that operational health does not prove the audience stays
within roughly 5–10 seconds of scheduled Spanish playback. The remaining live
promotion sequence is:

1. a fresh preflight and targeted Sample 02 pass from the recovery commit;
2. a new three-sample run through the resumable staged batch harness;
3. executed browser/Web Audio queue measurements at 1.00x, 1.05x, and 1.10x;
4. a synchronized English-phrase to audible-Spanish measurement; and
5. native-Spanish review of any playback/prosody acceleration.

## Selecting the backend path

The safe default remains the original monolithic Riva S2S operation:

```dotenv
S2S_PIPELINE_MODE=monolithic
```

Enable the direct staged path explicitly:

```dotenv
S2S_PIPELINE_MODE=staged
```

`start.sh` sources the repository `.env` before it launches FastAPI. For a
manual backend start, source `.env` in the shell first. Restart the backend
after changing the mode because configuration is loaded when the Python
process imports the application.

Confirm the active value before labeling a run:

```bash
curl --fail --silent http://localhost:8000/api/config | python3 -m json.tool
```

The response includes `pipelineMode` and the full `stagedConfig` used for the
run. `/` also includes `pipeline_mode`. The legacy `riva_connected` field on
`/` describes the monolithic client; staged clients are intentionally created
and connected per stream.

## WebSocket contract

Both modes continue to use the same endpoint and messages.

Client to server:

```json
{"type":"start_stream","targetLanguage":"es-US"}
{"type":"end_input"}
{"type":"stop_stream"}
{"type":"ping"}
```

Binary client frames remain 16 kHz mono Int16 English PCM. Binary server frames
remain 16 kHz mono Int16 translated Spanish PCM. JSON server messages retain
the existing `status`, `error`, `level`, and `pong` shapes.

The natural staged lifecycle is:

```text
connected
  -> start_stream
  -> connect fresh direct ASR/NMT/TTS clients
  -> listening
  -> receive English PCM
  -> end_input
  -> processing
  -> drain ASR finals, punctuation residual, NMT, and TTS
  -> send ordered Spanish PCM segments
  -> close workers and validate cleanup/sequence integrity
  -> completed
```

`listening` is emitted only after the staged pipeline has started successfully.
Duplicate `end_input` messages are harmless. `stop_stream`, client disconnect,
or replacement by another single-user session awaits staged cleanup.

## Ordering, cancellation, and terminal correctness

- Every staged stream gets fresh, session-owned direct clients. A cancelled
  RPC can close its retained channel without poisoning a later stream.
- One output relay consumes staged results in FIFO order. All WebSocket writes,
  including status, level, ping response, and PCM, use the same serializer.
- A synthesized segment is published atomically only after Magpie finishes the
  segment. Partial TTS output is never sent.
- Successful WebSocket sends are recorded by sequence ID and monotonic time.
- `completed` is not sent until all PCM sends have completed, the pipeline has
  closed, and its summary reports no cleanup errors or incomplete sequences.
- A model, send, cleanup, or sequence-integrity failure emits the existing
  `error` shape and never emits `completed` for that generation.
- A generation token prevents audio or terminal messages from an older stream
  leaking into a restarted stream.

TTS is never retried. NMT is normally a single request and never repeats an
unchanged request. One diagnosed short-segment case may issue a single
punctuation-removed request before TTS has started, while retaining the same
sequence ID and provenance. The second result must pass the full target-text
validator, so the recovery cannot duplicate audible speech. See
[Narrow NMT recovery for short punctuated segments](NMT_SHORT_SEGMENT_RECOVERY.md).

## Content-safety boundary

The full-sample attempts established a fail-closed boundary before Magpie:

- configured standalone hesitation fillers are suppressed by the segmenter
  before sequence ID allocation and counted in privacy-safe telemetry;
- narrow deterministic Spanish overrides apply only to a small fixed allowlist
  of standalone expressions observed to produce wrong-script NMT output;
- target text is normalized and validated immediately after NMT and
  defensively again before TTS; Spanish output must contain at least one
  letter or digit, every letter must be Latin script, and control/format
  characters, symbols, and detached marks are rejected;
- after a first-pass `TargetTextValidationError`, exact `es-US` input shaped as
  1-32 ASCII letters plus one `.`, `?`, or `!` may be requested once without
  that punctuation; and
- every ineligible or second-attempt invalid result terminates the session.
  There is no blind retry of unchanged NMT or TTS input.

Suppression happens before ID allocation. Once an ID exists, its translated
text/audio is never silently dropped: it must complete in order or make the
session fail.

## Exported staged evidence

`GET /api/test/export` retains its existing `events` array and adds a nullable
`stagedPipeline` object. For a staged run it contains the pipeline summary,
detailed stage events, and successful WebSocket send evidence. Shutdown now
retains an immediate snapshot before asynchronous cleanup and replaces it with
the finalized `closed` snapshot. The batch client waits through a bounded
close-settling interval for that final object rather than losing evidence in
the cleanup window.

Important fields include:

- `session_id`, `outcome`, `failure`, and `cleanup_errors`;
- emitted, completed, and incomplete sequence IDs;
- maximum queue depths and blocked-put counts;
- per-event segment identity, ASR-final provenance, and emission reason;
- NMT/TTS processing, first-audio, and queue-residence times;
- per-completed-NMT `retry_count` plus the summary `nmt_retry_count`;
- sequence/provenance and `retry_count=1` on an exhausted recovery error;
- audio bytes and duration;
- `websocket_sent_sequence_ids`; and
- `websocket_send_events` with sequence ID and monotonic send time.

The CLI batch harness stores the active `/api/config` snapshot and staged
summary in each result JSON. New-format captures freeze ASR/NMT/TTS endpoints,
image references, optional digests and profiles, model/voice, languages, EOU,
and word-time settings. They also cross-check successful server PCM sends
against client receives by both frame count and total bytes. These internal
queues are item counts. They are not interchangeable with seconds of listener
backlog.

The configuration fields are operator-declared backend provenance, not Docker
runtime introspection. Use and independently verify digest-qualified image
references for formal comparison runs. A historical summary without
`modelConfig` is auditable but is not resumable under the stricter schema.
Formal runs should also begin at a clean Git commit and retain each source
audio SHA from the harness manifest.

## Browser audience evidence

The normal translation panel displays the actual Web Audio schedule backlog,
including current and peak seconds, playback rate/mode, and entries above the
10-second limit. Stable `data-*` attributes make those values available to
browser automation.

The latency dashboard remains the authoritative detailed browser evidence. It
records queue samples and scheduled chunks in CSV. Natural test completion now
requires all three conditions:

1. the server emitted `status: completed`;
2. no translated PCM arrived for the configured quiet interval; and
3. the scheduled Web Audio queue reached zero.

A server error or a 300-second terminal timeout produces an explicit failed
run while preserving statistics and CSV export. The long-form harness retains
only an allowlisted generated CSV, summary, and plot for failed captures, with
neutral filenames, owner-only permissions, and manifest hashes under the
ignored run directory. It does not copy source or generated audio into that
failure record. Manual Stop remains a manual cancellation and does not wait
for natural completion.

## One-minute WebSocket preflight

With all three pinned NIMs healthy, launch the backend in staged mode and run
the existing terminal-aware client against the one-minute WAV:

```bash
S2S_PIPELINE_MODE=staged \
PYTHONPATH=.python-packages:backend \
python3 -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
```

In another shell:

```bash
PYTHONPATH=.python-packages \
python3 batch_latency_test.py \
  --file "${S2S_TEST_AUDIO_DIR:-test_audio}/preflight.wav" \
  --backend http://127.0.0.1:8000 \
  --output-dir test_results_staged/websocket-preflight
```

Before promotion, require:

- the reported mode is `staged`;
- the entire input was sent;
- translated audio is nonempty;
- exactly one natural completion follows all PCM;
- staged outcome is `complete`;
- the staged snapshot state is `closed`;
- cleanup and incomplete-sequence lists are empty;
- every completed NMT event has `retry_count` zero or one and their sum equals
  `nmt_retry_count`;
- dequeued and successfully sent sequence IDs match exactly; and
- every observed queue depth is within its recorded capacity.

The measured result belongs here only after the command completes. A direct
`staged_pipeline_smoke.py` success is useful but does not substitute for this
WebSocket gate.

### Live result: July 22, 2026

The terminal-aware WebSocket gate completed successfully on the RTX PRO 6000
VM. Port 8001 was used because an older backend already occupied port 8000;
the application configuration and model endpoints were otherwise unchanged.

| Measurement | Result |
|---|---:|
| Active mode and provenance | `staged` from `/api/config` |
| Source input | 60.000 s / 200 chunks, complete |
| First translated audio at client | 5.087 s |
| Translated output | 1,609,442 bytes / 50.295 s |
| Whole-prefix output/input ratio | 0.838x |
| Last-audio tail after input ended | 1.316 s |
| Completed-terminal arrival after input | 1.374 s |
| Harness drain observation (poll/settle included) | 2.254 s |
| Fixed-rate arrival-replay playback tail | 6.996 s |
| Segments | 23 emitted / synthesized / dequeued / sent |
| Emission reasons | 13 punctuation / 10 age fallback |
| Maximum NMT / TTS / output queue depth | 2 / 2 / 1 |
| Blocked NMT / TTS / output puts | 0 / 0 / 0 |
| Missing or incomplete sequence IDs | none |
| Stage, cleanup, WebSocket, or container failure | none |

Stage timing across the 23 segments:

| Stage measurement | Minimum | Average | Maximum |
|---|---:|---:|---:|
| NMT processing | 134.75 ms | 325.52 ms | 674.34 ms |
| TTS first response | 130.12 ms | 144.41 ms | 158.64 ms |
| TTS full completion | 203.00 ms | 474.04 ms | 887.35 ms |
| NMT queue residence | 0.13 ms | 13.83 ms | 176.32 ms |
| TTS queue residence | 0.13 ms | 2.87 ms | 62.28 ms |
| Output queue residence | 0.13 ms | 7.49 ms | 10.68 ms |

The batch integrity result was `passed: true`: outcome `complete`, empty
failure/cleanup/incomplete lists, contiguous sequence IDs 0-22, identical
dequeued and successfully sent IDs, exactly one terminal `completed` after all
23 PCM frames, no PCM after completion, and all queue depths within the
captured configuration. All three NIM containers remained healthy with zero
restarts.
The post-run GPU snapshot remained 32,217 MiB used and 65,034 MiB free out of
97,887 MiB.

This result is the hardened rerun after receive-order validation was added.
The raw JSON, CSV, plot, and runtime captures were reviewed before being
removed from public history under the repository sanitization policy. The
aggregate table above is retained; new raw captures stay in ignored local
runner output.

This is a short operational pass. The 6.996-second value is a fixed-rate replay
from actual client arrival times, not an executed browser/Web Audio queue
distribution or a marked-phrase delay. The full-sample operational pass below
shows why those audience measurements remain necessary.

## Full-sample promotion and audience gate

Sample 03 was used first because it is the shortest sample and historically
had the lowest playback pressure. Its operational hard gate passed on July 22,
2026; audience latency remains a separate gate.

| Measurement | Result |
|---|---:|
| Source input | 1,888.1045 s, complete |
| First translated audio at client | 5.112942 s |
| Output/source duration ratio | 1.016270617x |
| Last-audio arrival after input ended | 0.849578 s |
| Completed-terminal arrival after input | 1.743608 s |
| Harness drain observation (poll/settle included) | 2.254558 s |
| Fixed-rate arrival-replay playback tail | 64.037605 s |
| Sequence IDs | 646 contiguous IDs, 0–645 |
| Standalone fillers suppressed before ID allocation | 6 |
| Maximum NMT / TTS / output queue depth | 4 / 4 / 1 |
| Blocked NMT / TTS / output puts | 15 / 0 / 0 |
| Integrity, stage, cleanup, WebSocket, container errors | none |

The run had exactly one natural completion after all 646 PCM frames, no PCM
after completion, no missing or incomplete IDs, and no container restart or
GPU OOM. NMT's four-item queue reached capacity and blocked 15 puts as designed
instead of dropping or reordering work. The staged integrity result passed.

The output ratio was only 1.016x over the whole source, yet actual arrival
timing produced a 64.038-second fixed-rate playback tail. Bursts and local
expansion therefore matter even when the aggregate ratio is close to 1.00x.
The under-one-second last-audio arrival tail is not the listener tail and does
not answer when a translated joke becomes audible.

See [Sample 03 staged full-sample canary](LONG_FORM_03_STAGED_CANARY.md). Raw
event streams, client logs, plots, and runtime manifests are intentionally not
published; the report retains the aggregate measurements and integrity result.

The same hard gate now applies to Sample 01 and Sample 02:

Hard gate:

- no disconnect, timeout, container restart, or GPU OOM;
- complete nonempty input and output;
- one terminal completion and no stage/cleanup error;
- no missing, duplicate, or reordered sequence;
- queues remain within configured bounds; and
- config, telemetry, logs, and output artifacts are saved.

Audience/quality observations:

- browser queue p95, peak, and time above 10 seconds;
- listener playback tail;
- exact marked-phrase or joke delay;
- exposure to 1.05x and 1.10x playback; and
- native-Spanish-listener intelligibility and prosody feedback.

The 5-10 second playback queue remains a soft audience target, not a guaranteed
hard bound. If translated production persistently exceeds 1.10x consumption,
the application cannot preserve every word and also enforce a hard queue cap.
Browser queue distributions, synchronized marked-phrase delay, and native
Spanish review of 1.05x/1.10x playback remain pending.

### Post-canary hardening status

Attempt 3 predates the final evidence and lifecycle hardening. Its retained raw
events independently prove that its `completed` control followed `end_input`,
that all 646 server PCM sends reached the client with 61,402,404 bytes, and
that no PCM followed completion. The archived full summary does not contain
the newer `modelConfig` or top-level terminal-arrival fields and therefore is
historical, not a resumable new-format checkpoint.

After the live run, deterministic tests added rejection of premature
completion, automatic send/receive count-and-byte parity, full model
provenance freezing, exact terminal-arrival timestamps, and cancellation-safe
session cleanup. Unknown controls now claim one staged error terminal and
close the active pipeline; unsafe CJK punctuation in Spanish-target text is
rejected before Magpie. These edge changes have complete unit/integration test
coverage but have not yet been exercised by another full GPU sample canary.

## Rollback

To return to the previously tested path, set:

```dotenv
S2S_PIPELINE_MODE=monolithic
```

Restart only the FastAPI process. The pinned ASR, NMT, and TTS containers and
the browser protocol do not need to change.
