# Research Summary: Real-Time Translation Latency Analysis

## Overview

This document summarizes findings from latency testing of the real-time English-to-Spanish speech translation pipeline using NVIDIA Riva services. Six test runs were conducted on 2026-02-12, ranging from short (~20s) to long (~60s) source audio durations.

## Pipeline Architecture

```
Microphone/File -> Backend (WebSocket) -> Riva ASR (en-US) -> NMT -> TTS (es-US) -> Client (WebSocket) -> Speaker
```

- Audio format: 16kHz mono int16, 4800 samples/chunk (300ms)
- Translation model: `megatronnmt_any_any_1b`
- TTS voice: `Magpie-Multilingual.ES-US.Isabela`
- gRPC endpoint: configured with `RIVA_URI` (default: `localhost:50051`)

## Drift Definition

Drift measures how far behind the listener's ears are from the original speaker:

```
drift = input_source_position - output_playback_position
```

- `input_source_position`: seconds of source audio sent so far
- `output_playback_position`: seconds of translated audio the speaker has finished playing
- A drift of 10s means the listener is hearing what the speaker said 10 seconds ago

## Test Results

### Test Runs (2026-02-12)

| File | Duration | Samples | Final Drift | Peak Drift |
|---|---|---|---|---|
| `latency-test-20260212-145554.csv` | ~60s | 601 rows | - | - |
| `latency-test-20260212-154817.csv` | ~60s | 730 rows | - | - |
| `latency-test-20260212-155006.csv` | ~60s | 1,211 rows | - | - |
| `latency-test-20260212-160256.csv` | 60s | 1,355 rows | 18.17s | 22.85s |
| `latency-test-20260212-171544.csv` | ~20s | 151 rows | - | - |
| `latency-test-20260212-172120.csv` | 20s | 373 rows | 7.36s | 7.36s |

Detailed analysis was performed on the two most recent runs (160256 and 172120).

### Short Test (172120): ~20s Source Audio

- 67 drift samples over 19.8s of source audio
- Initial pipeline latency: **~5.1s** (first Riva response at t=4.88s)
- Final accumulating drift: **7.36s**
- 4 Riva response bursts observed (at ~5s, ~12s, ~14.4s, ~18.8s)

### Long Test (160256): 60s Source Audio

- 200 drift samples over 60s of source audio
- Initial pipeline latency: **~4.9s** (first Riva response at t=4.91s)
- Final accumulating drift: **18.17s**
- Peak accumulating drift: **22.85s** at t=46.8s (exceeded 20s warning threshold)
- ~8 Riva response bursts observed

Drift progression in the 60s test:

| Elapsed | Acc. Drift | Notes |
|---|---|---|
| 5s | 0s | First response burst arrives |
| 12s | 5.5s | Second burst, brief drop then resumes climbing |
| 26s | 12.8s | |
| 31s | 10.2s | Large burst temporarily reduces drift |
| 46.8s | 22.85s | Peak drift, exceeds warning threshold |
| 47.1s | 13.2s | Massive burst drops drift by ~10s |
| 60s | 18.17s | End of test |

## Key Findings

### 1. Stall-Burst Pattern

Riva processes audio in sentence-level batches. The ASR component accumulates input audio until it detects a sentence boundary (endpointing), then the full sentence flows through NMT and TTS. The result is a burst of translated audio delivered all at once.

Between bursts:
- Zero translated audio is produced
- Drift grows at ~1s per second of wall time (playback position is frozen)

During bursts:
- Several seconds of translated audio arrive within ~200-400ms
- Drift drops sharply as the playback queue fills

### 2. Burst Interval: 6-14 Seconds

The gap between response bursts corresponds to ASR sentence-boundary detection. Observed intervals range from ~6s to ~14s depending on the speech content. Longer sentences or hesitations produce longer gaps and larger drift spikes.

### 3. Drift Growth Rate: ~0.3s Per Second of Source Audio

The net drift growth — accounting for both stalls and bursts — averages approximately 0.3 additional seconds of drift per second of source audio. This means:

- After 1 minute: ~18s behind
- After 5 minutes: ~90s behind (projected)
- After 30 minutes: ~9 minutes behind (projected)

This growth rate comes from three compounding factors:

1. **Pipeline latency** (~5s fixed cost at startup)
2. **Spanish output expansion** (~6.4% longer than English input — Spanish words have more syllables)
3. **Processing overhead** at each sentence boundary (ASR finalization + NMT + TTS synthesis adds ~1-2s per utterance beyond the audio duration)

### 4. Backend Is Not the Bottleneck

The `audio_received` to `audio_to_riva` latency in the backend is consistently <1ms. The backend immediately forwards audio to Riva over gRPC. All latency originates in the Riva pipeline itself.

### 5. Old Drift Metric Was Misleading

The previous drift calculation (`wall_elapsed - cumulative_received_audio_duration`) with an `initialDriftRef` subtraction masked the true listener experience:
- It subtracted the initial ~5s pipeline latency, making drift appear to start at 0
- It measured when audio *arrived* at the client, not when it *played through speakers*
- Bursts of audio appeared as instant drift reduction, but the listener still hears them sequentially at 1x speed

The new playback-based metric (`input_position - output_playback_position`) reflects the actual listener delay.

## Potential Improvements

### More Aggressive ASR Endpointing

Shorter ASR utterances would produce more frequent, smaller bursts. Instead of waiting 6-14s for a sentence boundary, endpointing every 2-3s would keep drift bounded. Trade-off: shorter utterances may reduce translation quality since NMT has less context.

### Streaming/Incremental TTS

Instead of waiting for the full NMT output before starting TTS, begin synthesizing as partial translations become available. This requires a TTS system that supports incremental input.

### Sentence-Level Parallelism

Process the next ASR utterance while the previous one is still in NMT/TTS. Currently the pipeline appears fully sequential — the next burst doesn't start until the previous one completes.

### Adaptive Chunk Skipping

If drift exceeds a threshold, skip source audio to bring the listener back to near-real-time. This sacrifices completeness for immediacy — acceptable in some use cases (live events) but not others (samples, lectures).

## Test Infrastructure

The Test Dashboard (`frontend/src/components/TestDashboard.tsx`) provides:
- File-based audio source (WAV/MP3) for reproducible tests
- Real-time drift chart with warning (20s) and danger (30s) thresholds
- Input and output audio monitoring toggles (muted by default)
- CSV export of all timing events (client + backend)
- Automatic drain detection (stops after 30s of silence)

Consent-cleared audio stays local and ignored. The default neutral fixtures are
`test_audio/preflight.wav`, `test_audio/long-form-01.mp3` through
`long-form-03.mp3`, and `test_audio/long-form-03-30min.wav`. See
`test_audio/README.md`; `S2S_TEST_AUDIO_DIR` can select an external directory.
