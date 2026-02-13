# 30-Minute Latency Test Results — Test 2 (EndpointingConfig Applied)

**Date:** 2026-02-12
**Source file:** `timing-export-2026-02-13T00-13-14-972Z.csv`
**Input audio:** `test-30min.wav` (English sample, 16kHz mono Int16 PCM)
**Translation target:** es-US (Spanish)
**Riva endpoint:** riva-host:50051

---

## Change from Test 1

Added `EndpointingConfig` to the ASR config in `riva_client.py`:

```python
endpointing_config = riva_asr_pb2.EndpointingConfig(
    start_history=200,       # 200ms window to detect speech start
    start_threshold=0.5,     # 50% non-blank frames triggers start
    stop_history=600,        # 600ms silence triggers final result
    stop_threshold=0.7,      # 70% blank frames triggers end
    stop_history_eou=400,    # 400ms early end-of-utterance
    stop_threshold_eou=0.8,  # 80% blank for early EOU
)
```

**Hypothesis:** More aggressive silence detection would cause ASR to emit final results more frequently, reducing the 15-35 second stalls seen in Test 1.

---

## Session Overview

| Metric | Value |
|---|---|
| Wall-clock duration | 31.5 min (1,890s) |
| Chunks sent | 6,294 |
| Output packets received | 15,367 |
| Input audio duration | 31.5 min |
| Output audio duration | 33.5 min |
| Output/Input ratio | **1.063x** (Spanish is 6.3% longer) |
| First response latency | ~5.1 seconds |
| CSV rows | 64,982 |

## Head-to-Head Comparison: Test 1 vs Test 2

| Metric | Test 1 (default) | Test 2 (endpointing) | Change |
|---|---|---|---|
| Chunks sent | 6,294 | 6,294 | Same |
| Output packets | 15,379 | 15,367 | -12 (negligible) |
| Output/Input ratio | 1.064x | 1.063x | Negligible |
| First response latency | 4.9s | 5.1s | +0.2s (slightly worse) |
| Drift slope | -4.63 s/min | -4.66 s/min | Same |
| **Stalls > 3s** | **184** | **178** | **-3% (within noise)** |
| **Stalls > 5s** | — | — | — |
| **Stalls > 15s** | **15** | **22** | **+47% (WORSE)** |
| **Max stall** | **35.5s** | **37.2s** | **+1.7s (WORSE)** |
| Top 5 stalls | 35.5, 31.3, 28.4, 23.3, 21.6 | 37.2, 34.2, 31.2, 27.1, 26.8 | All larger |

## Stall Details

| Rank | Gap Duration | Wall Time (approx) | Session Minute |
|---|---|---|---|
| 1 | **37.2s** | — | — |
| 2 | **34.2s** | — | — |
| 3 | 31.2s | — | — |
| 4 | 27.1s | — | — |
| 5 | 26.8s | — | — |

## Drift Analysis

**Drift slope: -4.66 seconds per minute** (unchanged from Test 1's -4.63)

The system continues to over-produce translated audio at the same rate.

## Riva Output Cadence

| Gap Between audio_from_riva Events | Count |
|---|---|
| < 100ms | ~98.4% |
| 100ms - 500ms | small |
| 500ms - 1s | small |
| 1s - 5s | ~130 |
| 5s - 10s | ~80 |
| 10s - 20s | 43 |
| 20s - 40s | 11 |

Top 10 Riva output gaps match client-side stalls exactly — confirming stalls originate in Riva.

## Conclusion: EndpointingConfig Had No Effect

**Result: FAILED — no meaningful improvement.**

The EndpointingConfig controls silence-based utterance boundary detection, but:

1. **The speaker has very few pauses** — in continuous sample speech, there is minimal silence for the endpointer to detect, regardless of sensitivity settings.

2. **Severe stalls got worse** — stalls > 15s increased from 15 to 22, and max stall grew from 35.5s to 37.2s. This may be within normal variance or could indicate the more aggressive endpointing disrupted Riva's internal batching.

3. **All other metrics unchanged** — drift slope, output ratio, first response latency, and total stall count are all within noise of Test 1.

## Open Questions

1. **Does the S2S API actually pass `endpointing_config` through to its internal ASR?** The S2S pipeline may use its own ASR configuration and ignore client-provided endpointing settings.

2. **Is the stall caused by ASR, NMT, or TTS?** Without per-stage timing within Riva's internal pipeline, we can't determine which stage is the bottleneck.

3. **Would server-side `riva-build` configuration changes help?** Client-side config may be overridden by server defaults.

4. **Would a different approach work better?** Instead of S2S, using separate ASR → NMT → TTS API calls would give us control over each stage and let us translate on interim ASR results.
