# Stage-burst attribution

## Purpose

The long-form headless gate can show that a no-drop listener queue grew beyond
its target, but aggregate p95, peak, and tail values do not identify where the
growth originated. This offline analysis aligns privacy-safe schema-3 backend
telemetry with the exact protocol-v1 client frame arrivals and deterministic
adaptive schedule.

The report is intended to distinguish these associated signals:

- ASR finalization and segment emission cadence;
- NMT admission, queue residence, processing, and retry recovery;
- TTS admission, queue residence, first audio, completion, and frame cadence;
- output admission, queue residence, and WebSocket relay;
- client frame-arrival cadence and translated-media rate; and
- listener-queue growth over fixed wall-clock windows.

It is post-hoc descriptive attribution, not causal proof. Stage workers overlap,
so their durations must not be added as if they formed one serial critical
path.

## Required evidence

Each input consists of a matched `*_results.csv` and adjacent
`*_summary.json`. The loader fails closed unless the capture proves:

- staged telemetry schema 3;
- incremental TTS publication enabled and post-NMT subsegmentation disabled;
- a closed, complete pipeline with clean staged integrity;
- contiguous parent and frame identities;
- matching produced, dequeued, sent, wire-received, and CSV-received PCM;
- one reconciled parent-completion record per parent;
- audio metadata protocol v1 frame pairing and source-clock arithmetic; and
- valid stage ordering, queue accounting, processing durations, and retries.

The analysis reuses:

- `freshness_trace.load_parent_freshness_trace` for the CSV, wire, parent,
  frame, byte, terminal, and client-arrival contract; and
- `analyze_streaming_latency.load_summary_latency` for staged event,
  ASR-source, TTS, publication, and WebSocket-send integrity.

It then reads only a numeric whitelist from the additional NMT, TTS, and
output events.

## Run the analyzer

From the repository root:

```bash
PYTHONPATH=backend:.python-packages:. \
python3 analyze_stage_burst_attribution.py \
  experiment_results/<run-id>/repeat-01/*_results.csv \
  --window-seconds 30 \
  --top-window-count 5 \
  --json-output experiment_results/<run-id>/stage_burst_attribution.json \
  --markdown-output experiment_results/<run-id>/stage_burst_attribution.md
```

The default top-window report partitions client arrivals into aligned,
half-open intervals `[0, 30)`, `[30, 60)`, and so on. It includes the final
full grid bucket containing the capture tail, so every received frame belongs
to exactly one aligned interval. If capture ends within that bucket, its
remaining interval simply contains no later arrivals. The report ranks the
positive queue-growth intervals from that fixed grid.

Descriptive rolling statistics use separate complete 30-second windows starting
at every whole second. Those windows overlap by design and retain only starts
whose full observation window fits the analyzed capture extent. The JSON and
Markdown label the aligned and rolling associations separately.

## Clock and identity rules

Backend ASR, segmenter, NMT, TTS, output, and WebSocket-send timestamps share
the server monotonic clock. Durations within that domain can be subtracted.

Client receive timestamps use a separate client-relative clock. The analyzer
never subtracts a backend timestamp from a client timestamp. It joins the two
domains only by validated parent/frame identity and compares cadence using
within-domain intervals.

Parent identity is `sequence_id`; frame identity is
`(parent_sequence_id, audio_frame_id)`. Punctuation splitting creates
many-to-many ASR-final provenance, and each derived parent can inherit a whole
final's coarse source range. Source offsets are therefore freshness envelopes,
not word-accurate semantic boundaries.

## Stage intervals

The whole-sample report includes distributions for:

- source end to latest contributing ASR final;
- latest contributing ASR final to segment emission;
- segment emission to accepted NMT enqueue;
- NMT blocked-put time, queue residence, and processing;
- NMT completion to accepted TTS enqueue;
- TTS blocked-put time, queue residence, first-audio latency, and total
  processing;
- first to final TTS frame receipt;
- output blocked-put time and queue residence;
- output dequeue to WebSocket send;
- first to final client receipt;
- translated PCM duration and frame count; and
- NMT/TTS retry and atomic-fallback counts.

`blocked_put_ms` is producer waiting before queue admission. Queue residence
begins after admission, so the two intervals are disjoint.

Reported model-processing time can exclude adapter work between the enclosing
stage events. It must be non-negative and fit inside its monotonic timestamp
envelope, but it is not required to equal that whole envelope. Queue residence
does reconcile directly with enqueue/start timestamps within a 10 ms
tolerance.

TTS `first_audio` is the first raw PCM response observed by the adapter.
`frame_received` means enough PCM was accumulated to form the configured
incremental frame. An atomic-fallback parent is included in end-to-end
audience timing but excluded from direct incremental-publication benefit.

## Window metrics

For each fixed client-arrival window, the analyzer reports:

- adaptive queue depth at window start and end;
- net queue growth and peak queue;
- translated PCM seconds arriving and arrival rate versus real time;
- adaptive scheduled workload arriving and its rate versus real time;
- frame and unique-parent counts;
- maximum preceding client inter-frame gap; and
- distributions of stage signals associated with parents delivering frames
  in the window.

The scheduler is the exact registered 5/8/10-second,
1.00x/1.05x/1.10x no-drop policy. Queue depth is deterministic scheduled
digital playback, not measured DAC or acoustic output.

Rank correlations between queue growth and associated signals are reported as
descriptive diagnostics. A correlation does not identify an independent
causal contribution: long target text, generated duration, NMT/TTS processing,
and queue residence can all rise together.

## Privacy

JSON and Markdown outputs contain only neutral sample ordinals, relative
numeric timing, parent/frame counts, audio durations, and aggregate
distributions.

They contain no:

- transcript or translation text;
- audio or PCM;
- input filename or path;
- endpoint or URL;
- session identifier;
- source or target organization name; or
- wall-clock timestamp.

Raw stage events and per-parent text lengths are never serialized.

## Interpretation boundaries

- ASR source ranges do not prove a phrase or punchline boundary.
- Client scheduled starts do not prove physical audibility.
- A parent can span window boundaries; window membership is based on client
  frame arrival.
- Retry duration combines all attempts because attempt-level timings are not
  present.
- Queue capacities count work items, not seconds of translated audio.
- Pacing the same no-drop PCM can move backlog from the client to the server
  without reducing source-to-listener delay.
- A strict freshness bound is impossible whenever cumulative translated media
  exceeds playback capacity plus available buffer headroom unless the product
  permits faster playback, shorter output, summarization, omission, or speaker
  coordination.

Run a live preflight or canary only after an offline result identifies a
specific mechanism and the proposed change cannot merely hide backlog in
another stage.
