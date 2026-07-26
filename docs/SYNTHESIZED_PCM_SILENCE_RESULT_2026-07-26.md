# Synthesized low-energy PCM live result

## Result

The default-off synthesized low-energy PCM gate passed a formal 60-second
preflight and a promoted five-minute Sample 02 capture on the same clean
commit. Every translated frame, byte, parent, terminal, staged event, and
privacy declaration reconciled.

The primary -50 dBFS measurement found low-energy audio at synthesized parent
edges consistently:

| Capture | Synthesized PCM | Parents | Combined edges | Edge share | Edge p50 / p95 / max |
|---|---:|---:|---:|---:|---:|
| 60-second preflight | 52.803 s | 16 | 4.683 s | 8.87% | 0.295 / 0.408 / 0.408 s |
| Five-minute Sample 02 | 311.243 s | 84 | 26.129 s | 8.39% | 0.299 / 0.508 / 0.668 s |

This is enough duration to justify a conservative edge-compression
counterfactual. It is **not** enough to make edge compression the complete
audience-latency solution. Long translated parents still arrive in bursts
faster than a listener can consume them.

No audio was trimmed, accelerated, stored, or played differently in either
live capture.

## Fixed provenance

Both runs used commit:

```text
2d912d700413ed65a75d3604f4aa0133ef3bf535
```

| Service | Pinned image | Running digest |
|---|---|---|
| ASR | `nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0` | `sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850` |
| NMT | `nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2` | `sha256:3789b08b72c8dfbb09d1144e2bfd1f13c95911c2f997c9e11d81afb5aa90c9fb` |
| TTS | `nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0` | `sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d` |

The pipeline kept the registered 800 ms EOU, punctuation splitting, bounded
NMT/TTS queues, target language, Magpie voice, retry policy, schema-3
incremental publication, 500 ms output frames, and audio metadata protocol v1
unchanged.

## Integrity and measurement overhead

| Gate | 60-second preflight | Five-minute Sample 02 |
|---|---:|---:|
| Completed parents | 16 | 84 |
| Ordered PCM frames | 114 | 664 |
| Drops / reorders / duplicates | 0 / 0 / 0 | 0 / 0 / 0 |
| Staged integrity | Pass | Pass |
| Scan p95 / 5 ms limit | 0.891 ms | 0.901 ms |
| Scan maximum / 25 ms limit | 1.308 ms | 1.485 ms |
| Raw PCM retained | No | No |
| Per-frame scan timings retained | No | No |

The client measured each frame only after its receive timestamp and playback
schedule were recorded. Scan overhead was less than one millisecond at p95 in
both live runs and did not approach the preregistered perturbation limits.

## Threshold sensitivity

### 60-second preflight

| Threshold | Leading | Trailing | Combined edges | Edge share | Internal |
|---:|---:|---:|---:|---:|---:|
| -60 dBFS | 1.680 s | 2.348 s | 4.028 s | 7.63% | 5.740 s |
| **-50 dBFS primary** | **1.740 s** | **2.943 s** | **4.683 s** | **8.87%** | **8.600 s** |
| -40 dBFS | 1.780 s | 4.043 s | 5.823 s | 11.03% | 13.680 s |

### Five-minute Sample 02

| Threshold | Leading | Trailing | Combined edges | Edge share | Internal |
|---:|---:|---:|---:|---:|---:|
| -60 dBFS | 8.540 s | 12.609 s | 21.149 s | 6.79% | 34.960 s |
| **-50 dBFS primary** | **8.860 s** | **17.269 s** | **26.129 s** | **8.39%** | **50.660 s** |
| -40 dBFS | 9.220 s | 20.384 s | 29.604 s | 9.51% | 80.820 s |

At the primary threshold, all 84 five-minute parents had at least 100 ms of
combined edge low energy, 72 had at least 250 ms, and five had at least
500 ms. No parent was classified as all-low-energy.

These are windowed RMS classifications, not proof of silence. Internal
low-energy intervals can carry linguistic timing and are not candidates for
automatic removal.

## Audience result without intervention

The five-minute source produced 311.243 seconds of translated PCM, 11.243
seconds more than the 300-second source prefix. The existing adaptive
1.00/1.05/1.10x no-drop schedule retained all audio but did not meet the
bounded-queue objective:

| Metric | Fixed 1.00x | Existing adaptive policy |
|---|---:|---:|
| Listener tail | 30.317 s | 28.132 s |
| Time-weighted queue p95 | 16.981 s | 12.246 s |
| Peak queue | 25.556 s | 23.371 s |
| Time above 10 seconds | 192.260 s | 36.380 s |

The source-frontier projection also remained too late for a tightly timed live
event:

| Policy | Source end to scheduled parent start p50 / p95 / max |
|---|---:|
| Fixed 1.00x | 12.862 / 18.568 / 29.560 s |
| Existing adaptive | 8.135 / 13.461 / 27.480 s |

These are parent source-range proxies, not synchronized joke or phrase
landmarks. They nevertheless show that a listener can still be well behind
the source even when every service and transport integrity gate passes.

## Duration-only capacity counterfactual

The offline capacity replay kept every arrival timestamp and changed only
modeled media duration. It is deliberately optimistic: uniform scaling does
not reproduce where real leading and trailing samples occur, and it proves
neither safe removal nor listening quality.

### Primary edge ideal

Removing every primary-threshold edge would reduce modeled media duration by
26.129 seconds, a scale of `0.916050`.

| Playback | Media scale | Queue p95 | Peak queue | Time above 10 s | Tail |
|---|---:|---:|---:|---:|---:|
| 1.00x | 1.000000 | 16.981 s | 25.556 s | 192.260 s | 30.317 s |
| 1.00x | 0.916050 | 11.385 s | 22.917 s | 27.861 s | 27.678 s |
| 1.10x | 0.916050 | 9.113 s | 20.299 s | 13.436 s | 25.059 s |
| 1.50x, theoretical only | 0.916050 | 5.021 s | 13.317 s | 5.016 s | 18.077 s |

Even the unsafe all-edge ideal plus an unrealistic constant 1.50x fails the
10-second peak objective. Edge compression cannot by itself hard-bound the
listener queue.

### Guard-band sizing

The following rows retain the stated amount at both the leading and trailing
edge of every parent before calculating removable duration:

| Per-edge guard | Modeled removable duration | Share | Uniform capacity scale |
|---:|---:|---:|---:|
| 20 ms | 22.925 s | 7.37% | 0.926343 |
| 40 ms | 19.735 s | 6.34% | 0.936592 |
| 60 ms | 16.555 s | 5.32% | 0.946809 |
| 80 ms | 13.375 s | 4.30% | 0.957027 |
| 100 ms | 10.246 s | 3.29% | 0.967079 |

A 60 ms guard plus constant 1.10x modeled queue p95 at 9.809 seconds, but
peak queue remained 21.178 seconds and tail remained 25.938 seconds. This is a
capacity sizing result only, not a recommendation to deploy a 60 ms guard.

## Why the peak remains

The five-minute capture attributes the remaining peak to translated-parent
workload bursts:

| Metric | p50 | p95 | Maximum |
|---|---:|---:|---:|
| TTS audio per parent | 3.251 s | 8.731 s | 13.468 s |
| Parent immediate queue increase | 2.624 s | 7.379 s | 10.718 s |
| Source end to first client frame | 2.523 s | 4.702 s | 6.020 s |
| Output queue residence | 0.006 s | 0.010 s | 0.011 s |
| TTS-frame-ready to output enqueue | 0.001 s | 0.004 s | 0.008 s |

The strongest aligned 30-second interval delivered 40.403 seconds of
translated audio, an arrival rate of 1.347x, and grew the listener queue by
8.959 seconds. Output publication and WebSocket relay remained in the
millisecond range. The problem is not a stuck publisher; a long translated
parent becomes available much faster than it can be heard.

## Decision

Low-energy edge compression remains a promising supporting control because:

- the 8.39-8.87% edge share was consistent across both current captures;
- a guarded policy could remove enough modeled duration to offset much of the
  five-minute 11.243-second output expansion; and
- it reduces queue p95 and time above ten seconds in capacity replays.

It is not the primary architecture fix because:

- even the all-edge ideal does not bound peak or tail;
- 1.10x catch-up capacity is below the strongest short-window workload rate;
- Magpie multilingual TTS 1.7.0 has no documented native rate/prosody control;
  and
- quiet speech can be misclassified as low-energy.

## Recommended next gate

1. Build an **offline parent/frame-aligned edge counterfactual**. Allocate the
   measured leading duration to earliest frames and trailing duration to latest
   frames, preserve arrival timestamps, and sweep 40/60/80/100 ms guards plus a
   conservative per-edge removal cap. This is more faithful than uniform
   scaling and still changes no audio.
2. In parallel, design a way to reduce **semantic parent burst size without
   adding new per-request padding**. The earlier character-based TTS splitting
   increased total generated duration; the new edge evidence suggests repeated
   TTS request boundaries contributed to that penalty. Any new split design
   must reconcile child identity, preserve NMT context, and explicitly account
   for boundary padding.
3. Treat pitch-preserving 1.05/1.10x processing as a supporting arm only.
   Magpie exposes no native rate control, so this would be an external DSP
   intervention with full byte/duration telemetry and native-Spanish quality
   review.
4. Add synchronized reviewed source and translated-audible landmarks. Queue
   and parent-range proxies cannot directly certify the live joke-delay case.
5. Do not deploy trimming, internal-pause compression, higher rates, or
   content loss until the no-drop integrity gate and listening-quality gate
   pass.

If a 5-10 second bound must be absolute, product behavior for an overload that
cannot drain at an approved listening rate must be explicit. No-drop playback,
bounded latency, and a limited catch-up rate cannot all be guaranteed when
translated workload arrives above that capacity.

## Reproduce

From a clean checkout with the pinned Riva services already healthy:

```bash
CANARY_MODE=silence \
CANARY_DURATION_SECONDS=60 \
CANARY_INCREMENTAL_FRAME_MS=500 \
./run_streaming_tts_canary.sh
```

Promote only after the one-minute integrity/privacy gate passes:

```bash
CANARY_MODE=silence \
CANARY_SOURCE=test_audio/long-form-02.mp3 \
CANARY_DURATION_SECONDS=300 \
CANARY_INCREMENTAL_FRAME_MS=500 \
./run_streaming_tts_canary.sh
```

Private run summaries remain under ignored `experiment_results/`. This tracked
document contains only aggregate numeric evidence and neutral sample labels.
