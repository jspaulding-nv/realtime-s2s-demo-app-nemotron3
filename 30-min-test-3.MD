# 30-Minute Latency Test Results — Test 3 (Aggressive EndpointingConfig)

**Date:** 2026-02-12
**Source file:** `timing-export-2026-02-13T01-34-48-442Z.csv`
**Input audio:** `test-30min.wav` (English sample, 16kHz mono Int16 PCM)
**Translation target:** es-US (Spanish)
**Riva endpoint:** riva-host:50051

---

## Change from Test 2

Applied aggressive EndpointingConfig (values proven in 60s S2S controlled test):

```python
endpointing_config = riva_asr_pb2.EndpointingConfig(
    start_history=100,       # 100ms — very fast speech start detection
    start_threshold=0.3,     # 30% non-blank frames triggers start
    stop_history=300,        # 300ms silence triggers final result
    stop_threshold=0.5,      # 50% blank frames triggers end
    stop_history_eou=200,    # 200ms early end-of-utterance
    stop_threshold_eou=0.6,  # 60% blank for early EOU
)
```

**Hypothesis:** More aggressive silence detection would force ASR to finalize on very short pauses (~300ms), dramatically reducing max stall duration.

---

## Session Overview

| Metric | Value |
|---|---|
| Wall-clock duration | ~31.5 min |
| Chunks sent | 6,294 |
| Output packets received | ~15,400 |
| Input audio duration | 31.5 min |
| Output audio duration | ~33.5 min |
| Output/Input ratio | **~1.065x** |
| First response latency | ~5s |
| CSV rows | 65,438 |

## Three-Way Comparison

| Metric | Test 1 (default) | Test 2 (moderate) | Test 3 (aggressive) | T1→T3 Change |
|---|---|---|---|---|
| Chunks sent | 6,294 | 6,294 | 6,294 | Same |
| Output packets | 15,379 | 15,367 | ~15,400 | Same |
| Output/Input ratio | 1.064x | 1.063x | ~1.065x | Same |
| First response | 4.9s | 5.1s | ~5s | Same |
| Drift slope | -4.63 s/min | -4.66 s/min | -4.75 s/min | Same |
| **Stalls > 3s** | **184** | **178** | **203** | **+10% (worse)** |
| **Stalls > 5s** | **~100** | — | **122** | **+22% (worse)** |
| **Stalls > 10s** | **~50** | — | **34** | **-32% (better)** |
| **Stalls > 15s** | **15** | **22** | **6** | **-60% (better)** |
| **Stalls > 20s** | **~6** | — | **2** | **-67% (better)** |
| **Stalls > 30s** | **~2** | — | **1** | **(better)** |
| **Max stall** | **35.5s** | **37.2s** | **31.0s** | **-4.5s (better)** |
| Top 5 stalls | 35.5, 31.3, 28.4, 23.3, 21.6 | 37.2, 34.2, 31.2, 27.1, 26.8 | 31.0, 23.3, 17.8, 16.5, 16.0 | Tail compressed |

## Analysis

### What Improved (Severe Stalls)

The aggressive endpointing successfully compressed the worst-case tail:
- Stalls >15s dropped from 15 to 6 (-60%)
- Stalls >20s dropped from ~6 to 2 (-67%)
- Max stall reduced from 35.5s to 31.0s
- Top 5 stalls dramatically improved: the 5th worst went from 21.6s to 16.0s

### What Got Worse (Moderate Stalls)

More frequent moderate stalls appeared:
- Stalls >3s increased from 184 to 203 (+10%)
- Stalls >5s increased from ~100 to 122 (+22%)

This is expected: breaking long utterances into shorter segments means more individual NMT+TTS processing cycles, each with inherent latency.

### What Didn't Change (Drift)

Drift slope is virtually unchanged at -4.75 s/min (vs -4.63 in Test 1). Endpointing changes *when* Riva processes audio, not *how fast*. The fundamental processing overhead is unchanged.

## Conclusion

**Partial success.** Aggressive endpointing is the right direction — it significantly reduces catastrophic stalls (the user-experience-destroying 20-35s gaps). The trade-off of more frequent 3-10s gaps is acceptable since those are less disruptive.

However, the system still has fundamental throughput limitations:
- 203 stalls > 3s in a 31.5-minute session = ~6.4 stalls per minute
- Max stall still 31s (one remaining long gap)
- Drift continues accumulating at ~4.7s per minute

## Remaining Options to Explore

1. **Even more aggressive endpointing** — try `stop_history=200` or `stop_history=150` to cap max stall further
2. **Decomposed pipeline (ASR → NMT → TTS separately)** — would allow translating on interim ASR results and give per-stage control
3. **Server-side Riva configuration** — `riva-build` parameters might offer additional control
4. **Different ASR model** — Parakeet may have model-specific batching behavior
5. **Client-side buffering strategy** — smooth out stall-burst pattern with a playback buffer that absorbs gaps
