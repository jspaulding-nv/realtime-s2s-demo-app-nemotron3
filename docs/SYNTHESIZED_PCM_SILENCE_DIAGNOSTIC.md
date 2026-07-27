# Synthesized low-energy PCM diagnostic

## Decision this experiment supports

The current no-drop real-time pipeline can receive more translated media than
a fixed-rate listener can play during short bursts. Before changing speaking
rate, this diagnostic measures whether a meaningful part of each synthesized
parent is leading or trailing low-energy PCM. If so, a later bounded
edge-compression experiment may recover queue capacity with less effect on
spoken content than globally faster playback.

This experiment does **not** decide that measured audio is perceptually silent
or safe to remove. Low-energy speech can include unvoiced consonants, fades,
breaths, and room tone. Internal low-energy runs are reported for diagnosis
only and are not candidates for automatic removal.

## Why a new live capture is required

The recent publisher-handoff captures retained byte counts, durations, and
timing but deliberately retained no PCM or amplitude measurements. Duration
alone cannot distinguish speech from low-energy audio. The measurement
therefore has to run while validated translated PCM is transiently available.

Two older, non-current direct-pipeline captures provide only a directional
signal. At a -50 dBFS threshold, their combined leading and trailing
low-energy windows represented approximately 11.3% and 12.9% of synthesized
duration. Those captures do not have the current canary's complete image and
commit provenance, so they cannot answer the current decision gate.

## Measurement boundary

The default-off diagnostic runs in the batch client after all of the following
have happened:

1. an `audio_frame` header has passed protocol-v1 validation;
2. its binary PCM byte count has reconciled with that header;
3. the frame's receive timestamp and headless playback schedule have been
   recorded; and
4. the existing metadata-only observation hook has run unchanged.

The matching validated `audio_parent_complete` control closes each acoustic
parent. This measures the exact bytes delivered to a listener-facing client,
including publication and WebSocket framing, without adding work to ASR, NMT,
TTS, or the backend publisher.

## Fixed method

| Setting | Registered value |
|---|---|
| Encoding | signed 16-bit little-endian PCM |
| Sample rate | 16,000 Hz |
| Channels | one |
| Analysis window | non-overlapping 20 ms |
| Primary threshold | -50 dBFS RMS |
| Sensitivity thresholds | -60 and -40 dBFS RMS |
| Partial final window | classify its actual sample count at parent completion |
| All-low-energy parent partition | all samples assigned to leading; trailing and internal are zero |

Windows continue across transport-frame boundaries and reset only at a
validated parent boundary. This avoids treating the configured 500 ms
publication frame as an acoustic boundary.

For each parent and threshold, the accumulator retains only counts for active,
leading low-energy, trailing low-energy, and internal low-energy sample frames,
plus the longest internal run and internal-run count. It retains no PCM,
per-window energy trace, transcript, translation, file path, URI, source hash,
session identifier, or wall-clock timestamp.

Because scanning runs inline after the current frame has been timestamped and
scheduled but before the next receive, the capture also retains aggregate-only
scan duration. The formal timing gate requires scan p95 no greater than 5 ms
and maximum no greater than 25 ms. Per-frame timings are discarded. A local
60-second CPU microbenchmark over 120 synthetic 500 ms frames measured
0.295 ms p95 and 0.440 ms maximum; the live artifact must still pass its own
gate on the capture host.

The term **low-energy PCM** is used throughout the artifacts. The metric is not
a voice-activity detector and does not prove semantic or perceptual silence.

## Fail-closed integrity and privacy gate

A successful diagnostic must satisfy all of these conditions:

- staged telemetry schema 3 and incremental publication are active;
- audio metadata protocol v1 is negotiated;
- parent and frame IDs are unique, contiguous, and ordered;
- every diagnostic frame and byte reconciles with the protocol receive ledger,
  translated-response totals, parent-completion controls, and existing staged
  server evidence;
- the PCM format remains 16 kHz, mono, signed 16-bit little-endian;
- `sample_frames * channels * bytes_per_sample == audio_bytes`;
- active plus low-energy sample frames equals total sample frames;
- leading plus internal plus trailing equals low-energy sample frames;
- window counts include the final partial window exactly once;
- every configured threshold describes the same parents, frames, bytes, and
  sample counts;
- aggregate scan timing reconciles with the observed frame count, contains no
  per-frame values, and passes the 5 ms p95 / 25 ms maximum overhead gate;
- the diagnostic reaches one complete terminal state; and
- strict artifact allowlists and explicit privacy declarations pass.

Any diagnostic error aborts the capture with only the exception type in the
client-visible error. A partial diagnostic is never accepted as evidence.

## One-minute live gate

Run only from a clean commit with the pinned services already healthy:

```bash
CANARY_MODE=silence \
CANARY_DURATION_SECONDS=60 \
CANARY_INCREMENTAL_FRAME_MS=500 \
./run_streaming_tts_canary.sh
```

The harness:

- verifies the exact running image tags and digests;
- creates one shared 60-second source prefix under ignored
  `experiment_results/`;
- starts only its own schema-3 FastAPI process;
- preserves the existing 800 ms EOU, punctuation splitting, queues, retry
  policy, NMT, TTS voice, and 1.00x listener policy;
- captures the aggregate-only low-energy sidecar;
- runs the existing playback, streaming, burst, and freshness analyzers; and
- produces a separate synthesized-low-energy JSON and Markdown report.

Raw summaries stay in the ignored run directory. Only a reviewed,
aggregate-only result should be committed.

## Promotion and interpretation

Promote to a five-minute Sample 02 capture only when the one-minute run passes
all existing timing, no-drop, ordering, terminal, provenance, and privacy
gates. Evaluate the preregistered -50 dBFS result first; use -60 and -40 dBFS
only as a sensitivity range.

Useful decision quantities are:

- total leading and trailing low-energy duration and percentage;
- per-parent combined edge duration p50, p95, and maximum;
- parents above 100, 250, and 500 ms at either edge;
- internal low-energy duration, reported separately;
- the amount of listener queue that a duration-only edge-removal
  counterfactual could recover; and
- whether scanning overhead is negligible relative to the 500 ms publication
  interval.

Do not trim audio in this gate. If edge low-energy duration is material, the
next implementation must use conservative guard bands, preserve every spoken
sample outside the selected edges, expose full byte/duration telemetry, and
pass native-Spanish listening review. Internal pauses remain untouched.

If edge low-energy duration is too small to explain the burst, the next
candidate is a separately gated, pitch-preserving time-scale processor at
1.05x and 1.10x. Browser `AudioBufferSourceNode.playbackRate` is not
pitch-preserving and is not sufficient for that quality decision.

## Magpie rate-control constraint

The pinned Magpie multilingual TTS 1.7.0 gRPC request has no documented
speaking-rate, tempo, pause, or prosody field. NVIDIA's matching customization
documentation lists only the SSML `phoneme` tag for Magpie. The existing TTS
request should therefore remain unchanged in this measurement; any later
pitch-preserving time-scale processing must be described as an external
pipeline intervention, not a native Magpie control.

- [NVIDIA Speech NIM 26.02 TTS customization](https://docs.nvidia.com/nim/speech/26.02.0/tts/customization.html)
- [NVIDIA Speech NIM 26.02 TTS protobuf API](https://docs.nvidia.com/nim/speech/26.02.0/reference/api-references/tts/protos.html)
- [NVIDIA Speech NIM 26.02 release notes](https://docs.nvidia.com/nim/speech/26.02.0/about/release-notes.html)

## Live status

The formal 60-second gate and promoted five-minute Sample 02 capture passed on
July 26, 2026. See the
[aggregate live result](SYNTHESIZED_PCM_SILENCE_RESULT_2026-07-26.md).
