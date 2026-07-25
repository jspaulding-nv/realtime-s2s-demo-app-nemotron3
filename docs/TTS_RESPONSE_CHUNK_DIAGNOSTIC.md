# Atomic TTS response-chunk diagnostic

## Decision

Measure Magpie's server-streaming response cadence before changing what the
listener receives. The diagnostic is default-off:

```dotenv
STAGED_TTS_RESPONSE_CHUNK_TELEMETRY=0
```

When enabled, the direct TTS adapter still buffers the successful RPC and
publishes exactly one legacy PCM frame. It records only numeric response
identity, byte counts, and monotonic timing. It does not retain PCM,
transcripts, translated text, response metadata, input paths, endpoints, or
session identifiers in the derived report.

This keeps telemetry schemas 1 and 2, output ordering, and the atomic retry
contract unchanged. A failed first attempt remains private. If a genuine gRPC
`UNKNOWN` is retried, only metrics from the successful attempt are returned.

## Why this measurement came first

The five-minute unsplit control already showed that first TTS PCM existed
before the application sent the complete parent:

| Interval | p50 | p95 | Maximum |
|---|---:|---:|---:|
| TTS start to first response | 0.147 s | 0.172 s | 0.206 s |
| First TTS response to WebSocket send | 0.451 s | 2.104 s | 2.553 s |
| Complete TTS response to WebSocket send | 0.032 s | 0.071 s | 0.085 s |

The first-response-to-send interval is the most optimistic amount that
incremental forwarding can recover. Exact listener impact could not be
reconstructed because the control did not retain the sizes and arrival times
of responses after the first one.

## Implementation

The opt-in adapter records one `TTSResponseChunkMetric` for each nonempty Riva
response in the successful atomic attempt:

- zero-based response index;
- response and cumulative PCM bytes;
- response arrival monotonic time; and
- successful-attempt retry count.

`SynthesizedSegment` validates contiguous indices, nondecreasing timestamps,
retry attribution, and exact byte reconciliation. The staged session adds a
separate privacy-safe sidecar:

```text
staged_pipeline.tts_response_chunk_telemetry
```

The sidecar has its own schema version. Enabling it does not change the main
staged telemetry schema or WebSocket audio framing.

The analyzer validates the sidecar against TTS lifecycle and WebSocket
evidence, then emits a transcript-free JSON/Markdown report:

```bash
python3 analyze_streaming_latency.py \
  experiment_results/<run>/cap-0/shared-prefix_summary.json \
  --json-output experiment_results/<run>/cap-0/streaming_latency_analysis.json \
  --markdown-output experiment_results/<run>/cap-0/streaming_latency_analysis.md
```

## 60-second non-formal probe

A 60-second real-time probe used the pinned Nemotron 3 ASR, Riva Translate,
and Magpie TTS services with:

```dotenv
STAGED_TTS_SUBSEGMENT_MAX_CHARS=0
STAGED_TTS_RESPONSE_CHUNK_TELEMETRY=1
```

The staged integrity check passed with no terminal, retry, cleanup, ordering,
or PCM-parity error. The final cross-arm comparison did not run because the
original wrapper expected four subsegment arms; the runner now skips that
comparison for a one-arm diagnostic.

The automated runner command is:

```bash
CANARY_TTS_RESPONSE_CHUNK_TELEMETRY=1 \
CANARY_CAPS=0 \
CANARY_DURATION_SECONDS=300 \
./run_tts_subsegment_canary.sh
```

| Measure | Result |
|---|---:|
| Atomic TTS requests | 16 |
| Nonempty Riva PCM responses | 391 |
| Requests with multiple responses | 100% |
| Responses per request, p50 / p95 / max | 17 / 89 / 89 |
| PCM duration per response, p50 / p95 / max | 139 / 232 / 279 ms |
| Inter-response arrival, p50 / p95 / max | 19 / 32 / 45 ms |
| First response to RPC complete, p50 / p95 / max | 370 / 1,661 / 1,661 ms |
| Last response to RPC complete, p50 / p95 / max | 2 / 3 / 3 ms |
| First response to current WebSocket send, p50 / p95 / max | 402 / 1,738 / 1,738 ms |

Magpie therefore exposes genuinely incremental PCM in the pinned client and
container. The application, not the SDK, currently adds the full-response
hold.

## Audience interpretation

The same probe measured the following delays from each ASR final-level source
boundary:

| Event | p50 | p95 |
|---|---:|---:|
| ASR final | 0.850 s | 1.130 s |
| Segment emitted | 0.959 s | 2.901 s |
| First TTS response | 1.566 s | 3.609 s |
| Current WebSocket send | 1.962 s | 4.391 s |

Incremental forwarding can make a translated reaction begin sooner, and the
probe proves there are many useful frames to forward. Its likely improvement
is nevertheless hundreds of milliseconds to roughly two seconds for these
requests. It cannot eliminate time already spent waiting for ASR finalization,
age-based segmentation, NMT, a queued TTS request, or previously generated
Spanish audio to play.

The approximately 15.5-second first-audio value is measured from stream start,
not from the end of the first spoken idea. The first ASR final itself covered
source time 4.48-13.52 seconds. For audience questions such as a joke and
laughter, source-boundary-to-audible timing is the relevant metric.

Punctuation-split children currently inherit the time range of the whole ASR
final. These numbers are therefore not word-accurate joke markers. The batch
harness also sends 300 ms frames, so source alignment includes up to one frame
of quantization plus scheduling and transport effects.

## Five-minute promotion gate

The formal five-minute control passed. All 74 TTS requests returned multiple
PCM responses, producing 2,123 responses in total. The first response arrived
before the current atomic WebSocket send by 0.430 seconds at p50, 1.633 seconds
at p95, and 2.387 seconds at maximum. Exact response-byte and lifecycle
integrity passed with zero NMT/TTS retries or cleanup errors, and all three
pinned services remained ready after the run.

The detailed results, audience interpretation, reproducibility contract, and
measurement boundaries are in the
[five-minute response-cadence canary](TTS_RESPONSE_CHUNK_5MIN_CANARY_2026-07-24.md).

This promotes a default-off implementation experiment, not the staged route
itself. Use a new schema 3 rather than overloading schema-2 TTS subsequences:

1. Reframe variable Riva responses into frame-aligned 100 ms PCM blocks.
2. Identify each block as `(parent_sequence_id, audio_frame_id)`.
3. Commit blocks through a bounded thread-to-async bridge.
4. Enqueue a non-wire `PARENT_COMPLETE` marker only after clean iterator
   exhaustion; the marker carries authoritative frame and byte totals.
5. Retry a genuine gRPC `UNKNOWN` only before the first frame is committed.
6. After any committed frame, never retry or replay; preserve the successful
   prefix and emit one terminal error.
7. Keep schema-1/schema-2 behavior byte-compatible while the experiment is
   disabled.

This experiment targets response latency and burst shape. Holding the audience
near a bounded 5-10 second queue still requires playback/prosody catch-up or an
explicit overload policy when translated audio is produced faster than it can
be heard.
