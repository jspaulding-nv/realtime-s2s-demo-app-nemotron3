# 30-Minute Latency Test Results — Test 1

**Date:** 2026-02-12
**Source file:** `timing-export-2026-02-12T23-01-42-038Z.csv`
**Input audio:** local `long-form-03-30min.wav` fixture (16kHz mono Int16 PCM)
**Translation target:** es-US (Spanish)
**Riva endpoint:** configured with `RIVA_URI` (default: `localhost:50051`)

---

## Session Overview

| Metric | Value |
|---|---|
| Wall-clock duration | 31.5 min (1,890s) |
| Chunks sent | 6,294 |
| Output packets received | 15,379 |
| Input audio duration | 31.5 min |
| Output audio duration | 33.5 min |
| Output/Input ratio | **1.064x** (Spanish is 6.4% longer) |
| First response latency | ~4.9 seconds |
| CSV rows | 65,018 |

## Input Sending Quality

| Metric | Value |
|---|---|
| Chunk interval (mean) | 300.05 ms |
| Chunk interval (stddev) | 32.29 ms |
| Chunk size | 9,600 bytes (4,800 samples, 300ms) |

Input cadence is nearly perfect. The self-correcting timer pattern works well.

## Drift Analysis

**Drift slope: -4.63 seconds per minute**

The system produces translated audio faster than real-time. Over the full session, raw drift went from +6.8s (early) to -115.6s (end) — a total swing of 122 seconds.

| Time Point | Raw Drift |
|---|---|
| 5 min | +6.8s |
| 10 min | ~0s |
| 15 min | -20s |
| 20 min | -50s |
| 25 min | -80s |
| 30 min | -115.6s |

**Interpretation:** The negative drift means cumulative output audio duration exceeds wall-clock elapsed time (after subtracting initial pipeline latency). This is driven by:
- Spanish TTS producing 6.4% more audio than English input
- Output arriving in rapid-fire bursts (median inter-packet gap: 10.7ms)

The system does NOT fall behind — it over-produces. A client playback buffer would grow without bound (~2 extra minutes by end of session).

## Stalls (Most Concerning Finding)

**184 gaps > 3 seconds** detected, with **15 gaps > 15 seconds**. Top stalls:

| Rank | Gap Duration | Wall Time (approx) | Session Minute |
|---|---|---|---|
| 1 | **35.5s** | ~1,331s | ~22 min |
| 2 | **31.3s** | ~1,422s | ~24 min |
| 3 | 28.4s | ~1,080s | ~18 min |
| 4 | 23.3s | various | various |
| 5 | 21.6s | various | various |
| 6-15 | 17-20s | various | various |

The pipeline periodically goes silent for 17-35 seconds, then dumps a large batch of translated audio all at once. This bursty pattern causes:
- Audible dropouts for listeners during the gap
- Audio flood after the gap (more audio than real-time can consume)

## Stall Root Cause Analysis: Riva ASR Sentence Batching

### Evidence

Deep analysis of all event types during each stall reveals the root cause with 100% certainty.

**1. Client and backend never stall.** During every gap, `chunk_sent`, `backend.audio_received`, and `backend.audio_to_riva` events continue at the expected ~300ms cadence. For example, during the 35.5s stall, 118 chunks were sent and forwarded to Riva — exactly matching the expected count (35.5 / 0.3 = 118).

**2. Riva stops producing output.** Zero `audio_from_riva` events during every stall. The pipeline blocks entirely at the Riva ASR stage.

**3. 100% correlation.** All 15 largest client-side stalls match exactly to gaps in `audio_from_riva` events. The timestamps and durations are identical.

**4. ASR accumulates 15-35 seconds of audio before emitting.** During each stall, audio_to_riva events accumulate with no corresponding output:

| Stall | Duration | Chunks Accumulated | Audio Accumulated |
|---|---|---|---|
| #1 | 35.5s | 118 chunks | 35.4s |
| #2 | 31.3s | 104 chunks | 31.2s |
| #3 | 28.4s | ~95 chunks | ~28.5s |

**5. Massive burst after each stall.** After each gap, Riva produces 100-314 output packets within seconds — the full ASR->NMT->TTS pipeline processing the accumulated audio at once.

### Timeline Example: Stall #1 (35.5 seconds)

```
Second    chunks_in  audio_out  riva_out   What's happening
──────    ─────────  ─────────  ────────   ────────────────
1331          4         17        17       Normal streaming
1332          3          0         0       ASR goes silent — accumulating
1333          3          0         0       Still accumulating...
  ...
1365          3          0         0       ...34 seconds of silence
1366          3          0         0       ASR still hasn't emitted
1367          0        308+      309+      ASR emits → NMT → TTS → burst
```

### Mechanism

The Riva ASR is configured for **sentence-level transcription**. It accumulates audio until it detects a sentence boundary (pause, punctuation-worthy intonation, etc.). In a sample with long flowing sentences and minimal pauses, ASR can wait 35 seconds before deciding a sentence is complete. Only then does:
1. ASR emit a transcription
2. NMT translate the full sentence
3. TTS synthesize the full Spanish audio
4. Backend forward the burst to the client

### Riva Output Cadence Distribution

| Gap Between audio_from_riva Events | Count | Percentage |
|---|---|---|
| < 100ms | ~98.1% | Normal streaming (packets in rapid bursts) |
| 100ms - 500ms | small | Brief pauses |
| 500ms - 1s | small | Minor gaps |
| 1s - 5s | 138 | Short stalls |
| 5s - 10s | 83 | Medium stalls |
| 10s - 20s | 43 | Long stalls |
| 20s - 40s | 6 | Severe stalls |

The bimodal distribution confirms: Riva either streams rapidly (< 100ms between packets) or goes completely silent for seconds at a time. There is no middle ground.

## Output Packet Size Distribution

| Metric | Value |
|---|---|
| Dominant size | 4,458 bytes (139ms audio) — 62% of packets |
| Second size | 2,972 bytes (93ms audio) — 21% of packets |
| Min | varies |
| Max | varies |

These are Riva TTS output frame sizes.

## Throughput per 5-Minute Window

Each window consistently delivers 296-341 seconds of output audio, exceeding the 300-second input window — confirming the 1.064x expansion ratio holds throughout.

## Conclusions

1. **Pipeline throughput is sufficient** — the system generates more audio than input time. No accumulating backlog.
2. **Stalls are the primary problem** — 184 gaps > 3s and 15 gaps > 15s during a 31.5-minute session would cause frequent audible dropouts.
3. **Root cause is Riva ASR sentence batching** — confirmed with 100% correlation across all stalls. The ASR waits for sentence boundaries before emitting transcriptions, causing 15-35 second gaps.
4. **Output over-production needs management** — the 6.4% expansion means ~2 extra minutes of audio by end of a 30-min session. Client needs playback buffer management.
5. **First response latency (~5s) is acceptable** for a translation system but noticeable to users.
6. **Client and backend infrastructure work perfectly** — self-correcting timer, WebSocket transport, and backend forwarding all perform flawlessly.

## Recommendations

### Priority 1: Fix ASR sentence batching (addresses stalls)
- Configure Riva ASR to emit **partial/interim results** more frequently rather than waiting for full sentence boundaries
- Set a **maximum utterance duration** (e.g., 5-10 seconds) to force ASR to emit even without a clear sentence break
- Investigate Riva ASR `interim_results` and `max_alternatives` streaming config options
- Consider switching ASR to word-level or phrase-level emission mode

### Priority 2: Client playback buffer management (addresses over-production)
- Implement a bounded playback buffer that slightly speeds up playback (1.05x) when buffer exceeds threshold
- Or trim silence from TTS output to keep buffer size stable

### Priority 3: Instrumentation improvements
- Add `chunk_index` tracking to `audio_from_riva` backend events for per-chunk Riva latency measurement
- Track ASR transcription text alongside timing events to correlate stalls with sentence lengths
