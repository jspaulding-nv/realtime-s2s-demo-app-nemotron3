# Audio metadata observation protocol v1

## Purpose and rollout state

Protocol v1 makes translated PCM observable by parent and frame without
changing what the listener hears. It is an opt-in diagnostic for the staged
telemetry-schema-3 incremental-TTS path.

It does not buffer, reorder, accelerate, cancel, or drop audio. Legacy clients
that omit the opt-in continue to receive the same anonymous binary PCM. The
main live translation screen remains on that legacy path; the test dashboard
and CLI canary are the controlled observation clients.

The immediate questions it can answer are:

- Did every metadata header pair with exactly one binary PCM frame?
- Did every parent complete with the advertised frame and byte totals?
- How old was the corresponding source-media boundary when PCM reached the
  client?
- When would the same PCM begin in the browser's already-existing adaptive
  schedule?
- What would the validated 10-second, oldest-first whole-parent policy have
  skipped if it had been enabled?

The last item is a counterfactual only. Live audio remains lossless.

## Negotiation and capability discovery

The backend advertises supported versions in `GET /api/config`:

```json
{
  "audioMetadataProtocolVersions": [1]
}
```

Version 1 is advertised only when all of these are active:

- `S2S_PIPELINE_MODE=staged`;
- staged telemetry schema 3; and
- incremental TTS publication.

Otherwise the advertised list is empty.

The client opts in per stream:

```json
{
  "type": "start_stream",
  "targetLanguage": "es-US",
  "audioMetadataProtocolVersion": 1
}
```

The field must be the integer `1`. Booleans, strings, other integers,
monolithic mode, and non-schema-3 staged configurations fail closed. Omitting
the field selects legacy raw-binary behavior. Negotiation resets for every new
stream and is scoped to one WebSocket connection.

## Wire format

Under the server's one serialized send lock, every translated PCM message has
this exact order:

```text
audio_frame JSON
matching binary PCM
...
audio_parent_complete JSON
```

An `audio_frame` example:

```json
{
  "type": "audio_frame",
  "protocolVersion": 1,
  "streamGeneration": 1,
  "parentSequenceId": 0,
  "audioFrameId": 0,
  "audioBytes": 3200,
  "sampleRateHz": 16000,
  "channels": 1,
  "bytesPerSample": 2,
  "sourceStartMs": null,
  "sourceEndMs": 1800.0
}
```

Its binary message must be exactly `audioBytes` bytes and align to
`channels * bytesPerSample`.

After the final frame for a parent:

```json
{
  "type": "audio_parent_complete",
  "protocolVersion": 1,
  "streamGeneration": 1,
  "parentSequenceId": 0,
  "audioFrameCount": 12,
  "audioBytes": 38400,
  "sourceStartMs": null,
  "sourceEndMs": 1800.0
}
```

The completion marker is sent only after the server reconciles the parent. It
must precede the next parent's frame header.

The payload deliberately excludes transcript text, translation text, audio
content, source names, paths, endpoints, organization names, user identity,
session UUIDs, and wall-clock timestamps.

## Source-offset meaning

`sourceStartMs` and `sourceEndMs` are media offsets from input PCM sample zero.
They are not server or client timestamps, and each endpoint is independently
nullable.

With ASR word timing enabled, a non-null range normally represents the
contributing words' source range. With the current default word timing
disabled, the ASR adapter can emit:

```text
sourceStartMs = null
sourceEndMs   = audio_processed
```

That end-only value is a useful coarse processing boundary. It is not a
semantic utterance or punchline boundary and is labeled accordingly in
captures.

## Clock model

All client-side calculations stay in one monotonic clock domain. For the
real-time file harness:

```text
S0 = client-monotonic time assigned to input PCM sample zero
E  = sourceEndMs
R  = client-monotonic time when the binary message is received

source-end-to-receipt = R - (S0 + E)
```

For browser scheduling:

```text
P = performance.now() sampled at scheduling
C = AudioContext.currentTime sampled with P
A = scheduled AudioContext start time

projected scheduled start in client clock
  = P + 1000 * (A - C)

source-end-to-projected-scheduled-start
  = projected scheduled start - (S0 + E)
```

The browser value is a projected scheduled playback start. It is not proof of
DAC output or acoustic audibility. Never subtract backend monotonic values,
backend wall-clock values, client `performance.now()`, or media offsets across
clock domains.

The CLI file harness records its self-correcting send-loop anchor and source
sample ledger. A live microphone still needs an AudioWorklet sample-zero marker
and continuous sample count before the same source-boundary metric is
defensible.

## Receiver invariants

Negotiated clients reject a capture if any of these invariants fail:

- one exact header immediately precedes one binary message;
- the binary length equals `audioBytes`;
- generation is positive and constant within a stream;
- parent IDs start at zero and are contiguous;
- frame IDs start at zero and are contiguous within each parent;
- PCM format remains stable for the stream, and source range remains stable
  within a parent;
- no new parent begins before the prior completion marker;
- completion frame and byte totals reconcile;
- no ordinary control, second header, completion, or terminal interrupts a
  pending header/binary pair;
- a completed terminal has no pending header or incomplete parent;
- no metadata or PCM follows that terminal.

Unexpected metadata in legacy mode and anonymous binary in negotiated mode
also fail closed. An error capture can retain partial numeric evidence for
diagnosis but cannot pass the operational acceptance gate.

## Observation-only 10-second policy

The offline analyzer replays the same causal 1.00x/1.05x/1.10x scheduling
policy, then evaluates a separate shadow queue:

- target: 10 seconds;
- cancellation guard: 100 ms;
- candidate: oldest complete parent first;
- eligible only when all retained frames for that parent have not begun and
  start strictly after the decision time plus the guard;
- incomplete, already playing, partially played, and guarded parents remain;
- after a hypothetical removal, retained future frames are compacted without
  changing playback-rate decisions already made;
- every residual over-target condition is reported.

Fields are named `hypothetical_*` or `would_drop_*`. They never claim that
live speech was dropped. Whole-parent removal cannot guarantee the target when
the retained remainder is incomplete, audible, protected, or itself too long.

## CLI capture

Start a schema-3 backend and run a short preflight:

```bash
python3 batch_latency_test.py \
  --preflight \
  --backend http://127.0.0.1:8000 \
  --audio-metadata-protocol-v1
```

Run one file:

```bash
python3 batch_latency_test.py \
  --file test_audio/long-form-01.mp3 \
  --backend http://127.0.0.1:8000 \
  --output-dir experiment_results/metadata-v1 \
  --audio-metadata-protocol-v1
```

Run all three configured samples by omitting `--file`.

The summary's `audio_metadata_observation` object records protocol/generation
identity, the client-monotonic sample-zero offset, reconciliation counts, and
source-end-to-receipt availability and distributions. The CSV adds only
numeric frame, source-offset, and receipt-delay columns.

Replay the captured schema-3 trace without changing live playback:

```bash
python3 analyze_freshness_cap.py \
  --results-csv experiment_results/metadata-v1/long-form-01_results.csv \
  --summary-json experiment_results/metadata-v1/long-form-01_summary.json
```

The matched incremental-publication canary negotiates v1 only for its schema-3
arm:

```bash
CANARY_DURATION_SECONDS=60 ./run_streaming_tts_canary.sh
```

The schema-1 atomic control remains legacy. This proves the opt-in did not
silently alter the control arm.

The matched-canary comparator now treats the v1 run-info key, backend
capability, and observation summary as required formal-gate evidence. It
intentionally rejects historical canary directories created before this
protocol. Keep their already-generated reports or use the repository commit
that created them if they must be summarized again.

## Promotion gates

Before enabling any lossy browser behavior:

1. Pass unit tests for negotiation, pairing, ordering, restart, failures, and
   terminal cleanup.
2. Pass a 60-second schema-3 live observation canary with no dangling state.
3. Pass the five-minute shared-prefix canary and reconcile wire, server, CSV,
   parent, frame, and byte evidence.
4. Run all three long-form samples and review source freshness, queue,
   hypothetical loss, residual breaches, and translated-audio quality.
5. Add synchronized semantic markers and output loopback for true joke or
   marked-phrase delay.
6. Only then consider a separately gated short-lookahead parent scheduler.

The audience objective remains a bounded queue of roughly 5-10 seconds, not a
promise that every translated phrase is acoustically audible within 10
seconds. Source segmentation, ASR, NMT, TTS, transport, playback, and device
latency all contribute.
