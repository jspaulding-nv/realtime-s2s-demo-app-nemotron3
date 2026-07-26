# TTS publisher-handoff diagnostic

## Purpose

The formal browser-independent traces showed that translated-media bursts,
rather than ordinary first-frame availability alone, immediately tracked
listener-queue growth. They also exposed rare gaps between a TTS frame becoming
ready and that frame entering the bounded output queue. This diagnostic splits
that previously bundled interval without changing generated speech, PCM
framing, queue capacity, playback policy, or WebSocket ordering.

The measurement is default-off. It becomes active only when both existing
schema-3 incremental publication and response-chunk telemetry are enabled:

```dotenv
STAGED_TTS_INCREMENTAL_PUBLISH=1
STAGED_TTS_RESPONSE_CHUNK_TELEMETRY=1
```

The backend then reports
`ttsPublisherHandoffTelemetryEnabled=true` in `/api/config` and
`tts_publisher_handoff_telemetry_enabled=true` in the retained staged summary.
Older schema-3 evidence without that marker remains valid legacy evidence, but
it cannot answer this narrower handoff question.

## Timing chain

For every successfully committed `(parent_sequence_id, audio_frame_id)`, the
diagnostic validates this monotonic server-clock order:

1. the TTS response provided enough PCM to form the frame;
2. the blocking TTS worker requested publication;
3. the event-loop callback began;
4. the output-capacity semaphore acquisition completed and the callback resumed;
5. the frame-enqueue boundary was recorded;
6. the relay dequeued the frame;
7. binary WebSocket sending began; and
8. the successful WebSocket send completed.

The derived report decomposes the former frame-ready-to-output gap into:

- frame ready to publication request as a serial total;
- time a ready frame waited for the prior frame to commit;
- time from the later of frame readiness or prior-frame commit to the
  publication request;
- publication request to event-loop callback;
- callback to output-capacity acquisition/resumption;
- capacity acquisition to enqueue;
- enqueue to dequeue;
- dequeue to WebSocket-send start, including relay scheduling, writer-lock
  acquisition, lifecycle/metadata validation, and the metadata-header write; and
- WebSocket-send start to completion.

The callback-to-capacity interval includes semaphore-task scheduling and event-
loop resumption overhead. A nonzero value is therefore not, by itself, proof
that the bounded output queue was full.

Atomic-fallback parents are retained for byte/order integrity but excluded from
direct incremental handoff interpretation because their frames are deliberately
published only after the full TTS response completes.

## Privacy boundary

Raw diagnostic evidence is retained only under a Git-ignored output root; the
default is the ignored `experiment_results/` tree. New timing fields contain
only:

- neutral parent and frame order keys;
- monotonic numeric timestamps and derived durations;
- PCM byte/sample counts;
- queue depth/capacity;
- retry count; and
- fixed feature flags.

The diagnostic does not add transcript or translation text, PCM/audio payloads,
response metadata, paths, endpoints, external request identifiers, client
addresses, thread identifiers, speaker identity, or organization identity.
Only reviewed aggregate results belong in tracked documentation.

## Automated one-minute preflight

Prerequisites:

- the pinned ASR, NMT, and TTS containers are already running and ready;
- the current implementation is committed in a clean worktree;
- `.env` contains any required local runtime settings; and
- the selected source exists under `test_audio/`.

Keep each `ASR_IMAGE`, `NMT_IMAGE`, and `TTS_IMAGE` value as the exact pinned
tag recorded in the running container. Put the corresponding SHA-256 in the
separate `*_IMAGE_DIGEST` variable. Replacing the image tag with an
`@sha256:...` reference will correctly fail attestation if the existing
container records the tagged reference.

The runner loads `.env`, verifies container readiness and immutable image
digests, creates one exact source prefix, owns only its temporary FastAPI child,
and runs the existing unsplit one-request-per-parent schema-3 path:

```bash
CANARY_MODE=handoff \
CANARY_DURATION_SECONDS=60 \
CANARY_INCREMENTAL_FRAME_MS=500 \
CANARY_SOURCE=test_audio/long-form-01.mp3 \
./run_streaming_tts_canary.sh
```

The 500 ms frame setting matches the registered profile used by the retained
three-sample publisher-gap baseline. Handoff mode also defaults to 500 ms when
the variable is omitted; the explicit setting above makes the comparison
profile reviewable from the command itself. `ALLOW_DIRTY_CANARY=1` may bypass
only the clean-worktree requirement for a non-formal probe. The output root must
always be ignored by Git so raw evidence cannot be written into a tracked tree.

The one-minute preflight must fail closed unless all of the following hold:

- staged schema 3, response-chunk telemetry, publisher-handoff telemetry, and
  audio-metadata protocol v1 were active;
- the pipeline completed cleanly with no incomplete parent or cleanup error;
- produced, enqueued, dequeued, sent, and client-received frame identity and
  byte totals reconcile exactly;
- no committed frame is missing a handoff boundary;
- every boundary is finite, nonnegative, and correctly ordered;
- every positive `blocked_put_ms` reconciles with the measured
  callback-to-capacity acquisition/resumption interval, while zero preserves
  the legacy meaning that the queue was not observed full;
- there is no dropped, duplicated, replayed, or reordered PCM; and
- the aggregate analyzer output contains no raw private field.

The ignored result is written below a directory named like:

```text
experiment_results/publisher-handoff-canary-<UTC>-<commit>/streaming/
```

Review at least:

```text
shared-prefix_summary.json
streaming_latency_analysis.json
streaming_latency_analysis.md
stage_burst_attribution.json
stage_burst_attribution.md
schema3_freshness_cap_analysis.json
schema3_freshness_cap_analysis.md
backend.log
```

The streaming-latency report contains the new per-frame handoff-component
distributions. The stage/burst report supplies the matched stage, queue,
frame-ready-to-enqueue, translated-media-arrival, and listener-queue context
needed to compare the diagnostic with the retained baseline.

## Five-minute diagnostic

Only after the one-minute preflight passes from the same clean commit, run the
five-minute diagnostic on Sample 02, which had the strongest retained anomaly:

```bash
CANARY_MODE=handoff \
CANARY_DURATION_SECONDS=300 \
CANARY_INCREMENTAL_FRAME_MS=500 \
CANARY_SOURCE=test_audio/long-form-02.mp3 \
./run_streaming_tts_canary.sh
```

For coverage across all three neutral samples, repeat that five-minute command
sequentially with `CANARY_SOURCE` set to `long-form-01.mp3`,
`long-form-02.mp3`, and `long-form-03.mp3`. Keep the commit, duration, frame
size, model digests, and all other settings fixed.

Compare the new component distributions with the retained unsplit baseline:

- frame-ready-to-enqueue p50, p95, maximum, and count over 100 ms / 1 second;
- listener-queue p95, peak, time above 10 seconds, and tail;
- source-boundary-to-first-frame and scheduled-playback-start timing;
- NMT, TTS, and output blocked admission;
- TTS response cadence and generated-audio duration; and
- terminal, retry, cleanup, frame, byte, and no-drop integrity.

The previous three-sample baseline observed 2 / 9 / 2 frame-ready-to-enqueue
gaps over 100 ms, including 1 / 7 / 0 over one second, with per-sample maxima
of 5.648 / 7.054 / 0.930 seconds. Those values are measurement targets, not
proof of a publisher defect.

## Decision rule

Interpret the component carrying most of a repeated long tail:

| Dominant interval | Primary interpretation |
| --- | --- |
| Prior-frame serialization wait | propagated delay from an earlier frame, including any earlier output backpressure |
| Later of ready/prior commit → publication request | adapter/frame-builder or blocking-worker work after publication can proceed |
| Request → callback | thread-to-event-loop dispatch delay |
| Callback → capacity/resumption | semaphore-task scheduling and event-loop resumption; if independently shown full, also bounded output backpressure |
| Capacity → enqueue | local commit/bookkeeping delay |
| Enqueue → dequeue | output queue residence |
| Dequeue → send start | relay scheduling, serialized writer-lock wait, validation, and metadata-header write |
| Send start → completion | WebSocket transport/backpressure |

Implement a handoff fix only if the new timestamps show that one of these
intervals is material and repeatable. Reject a change that drops or duplicates
PCM, changes content order, creates unbounded waiting, or merely moves the same
backlog to another queue.

If the handoff is not material, measure leading/trailing synthesized silence
and then test a supported native TTS rate control or pitch-preserving 1.05x /
1.10x time-scale arm. Native-language review remains required for prosody,
intelligibility, and translation continuity before promotion.

The 2026-07-26 one-minute preflight and promoted five-minute Sample 02 run
completed without a publisher-handoff interval over 100 ms, while the no-drop
listener queue still missed its 5/10-second objective. See
[TTS publisher-handoff live result](PUBLISHER_HANDOFF_RESULT_2026-07-26.md).
