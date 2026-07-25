# Atomic TTS response cadence: five-minute canary

## Decision

Proceed to a default-off incremental TTS publication experiment while keeping
post-NMT request splitting disabled:

```dotenv
STAGED_TTS_SUBSEGMENT_MAX_CHARS=0
STAGED_TTS_RESPONSE_CHUNK_TELEMETRY=0
```

The formal canary passed the promotion gate for that experiment. All 74 Magpie
requests returned multiple PCM responses, the first response arrived much
earlier than RPC completion, response bytes reconciled exactly with the
published parent audio, and the run had no NMT retry, TTS retry, lifecycle
error, cleanup error, or service-health regression.

This is an implementation decision, not an audience-latency pass. Incremental
publication can reduce the server-side hold after TTS begins, but it cannot
remove ASR finalization, segmentation, NMT, TTS queueing, or already-buffered
listener audio.

## Evidence contract

The runner used one 300-second, 16 kHz mono prefix in real time from clean
commit `11fbb58`. Its neutral fixture-prefix digest was:

```text
78e04698bf76502bc1ae23c5dac8391de0f60a51dc13aae9f042532a24df44d4
```

The staged pipeline used:

- Nemotron ASR Streaming `1.2.0`, English `batch_size=32`;
- Riva Translate 1.6B `1.5.2`;
- Magpie multilingual TTS `1.7.0`;
- 800 ms ASR EOU;
- one unsplit TTS request per translated parent; and
- response-cadence telemetry enabled without partial PCM publication.

The runner sent 1,000 identical-size source frames and completed normally. The
schema-aware integrity check found no missing, duplicate, reordered, or
incomplete parent. All 74 emitted segments produced and sent exactly one
atomic WebSocket audio message. Maximum NMT/TTS/output queue depths were
3/4/1, with no blocked queue put. The sidecar's final cumulative byte counts
sum to the exact 9,384,648 bytes received by the test client.

The ignored raw evidence directory contains audio, logs, configuration
snapshots, and detailed numeric telemetry. This document retains only
sanitized aggregate evidence.

## TTS response cadence

Magpie exposed genuinely incremental PCM on every request:

| Measure | Result |
|---|---:|
| Atomic TTS requests | 74 |
| Nonempty PCM responses | 2,123 |
| Requests with multiple responses | 74 / 74 (100%) |
| Responses per request, p50 / p95 / max | 22 / 82 / 109 |
| Request start to first PCM, p50 / p95 / max | 142 / 173 / 230 ms |
| PCM duration per response, p50 / p95 / max | 139 / 232 / 372 ms |
| Inter-response arrival, p50 / p95 / max | 19 / 31 / 54 ms |
| First response to RPC complete, p50 / p95 / max | 401 / 1,561 / 2,301 ms |
| Last response to RPC complete, p50 / p95 / max | 2 / 3 / 4 ms |

The SDK and service are therefore not forcing a single complete waveform.
The current application creates the hold by joining all responses before
publishing one parent frame.

## Source-boundary latency

Each value below is measured from the end boundary of the contributing ASR
final:

| Event | p50 | p95 | Maximum |
|---|---:|---:|---:|
| ASR final | 0.875 s | 1.057 s | 1.218 s |
| Segment emitted | 0.957 s | 2.978 s | 3.241 s |
| First TTS PCM available | 1.795 s | 4.006 s | 5.590 s |
| Full TTS response available | 2.463 s | 4.989 s | 5.692 s |
| Current atomic WebSocket send | 2.509 s | 5.005 s | 5.714 s |
| First TTS PCM to current send | 0.430 s | 1.633 s | 2.387 s |

The paired first-PCM-to-send interval is the useful upper bound for this
experiment. Incremental publication could recover a median 430 ms and p95
1.63 seconds for these requests. Browser scheduling, frame assembly, and an
optional unpublished safety prefix mean actual microphone-to-ear improvement
will be smaller.

The initial atomic WebSocket message arrived 15.49 seconds after stream start,
but that is not a 15.49-second completed-thought delay. Its contributing
source boundary was at 13.52 seconds, and the message was sent 1.91 seconds
after that boundary. The first-PCM hold within that path was 0.309 seconds.

## Long-run audience interpretation

The five-minute workload still demonstrates why streaming TTS alone cannot
guarantee a live-audience objective:

| Measure | Result |
|---|---:|
| Source duration | 300.000 s |
| Synthesized duration | 293.270 s |
| Output/input duration ratio | 0.9776x |
| Average whole-run drift | 21.490 s |
| Maximum whole-run drift | 47.155 s |
| Fixed 1.00x listener tail | 40.703 s |
| Adaptive 1.00x/1.05x/1.10x listener tail | 31.601 s |
| Adaptive time-weighted queue p95 / peak | 21.260 / 29.084 s |
| Adaptive time above 10 seconds | 73.534 s (23.3% of playback window) |
| Source audio exposed to accelerated playback | 87.3% |

Although total synthesized audio was shorter than the source in this run,
bursty delivery still created a large playback queue. A live listener could
therefore hear the translated version of a joke after the room has already
reacted. Forwarding the first TTS PCM sooner improves local responsiveness but
does not catch up audio that is already queued.

The working audience target remains a bounded queue of approximately 5-10
seconds. The present no-drop 1.05x/1.10x controller did not hold that bound,
so prosody/playback-speed quality testing and an explicit overload policy
remain separate required work.

## Measurement boundaries

- Punctuation-split children inherit the source range of their complete ASR
  final, so the boundaries are not word-accurate joke markers.
- The harness dispatches 300 ms frames in real time. Alignment includes up to
  one frame of quantization plus scheduler and transport effects.
- First-PCM-to-send is an upper bound on recoverable application delay, not a
  direct microphone-to-ear measurement.
- This run is not a matched comparison with earlier stochastic synthesis
  runs. Differences in total generated duration or listener queue across
  separate runs must not be attributed to the telemetry flag.
- Native-language listening review is still required before promoting any
  playback-speed or prosody policy.

## Next implementation gate

Implement the experiment under a new telemetry/wire schema 3, disabled by
default:

1. Reframe variable Riva responses into frame-aligned 100 ms PCM blocks.
2. Identify each block by `(parent_sequence_id, audio_frame_id)`.
3. Move blocks through a bounded thread-to-async bridge that preserves order
   and backpressure.
4. Emit a private `PARENT_COMPLETE` marker only after clean iterator
   exhaustion, with authoritative frame and byte totals.
5. Retry a genuine gRPC `UNKNOWN` only before the first frame is committed.
6. After any committed frame, never retry or replay it; preserve the prefix
   and terminate the affected stream explicitly.
7. Keep schema-1 and schema-2 behavior byte-compatible while schema 3 is off.
8. Compare atomic and streaming modes on one short probe, then a matched
   five-minute canary, using source-boundary-to-first-send, queue p95, tail,
   exact byte parity, and failure semantics as gates.
