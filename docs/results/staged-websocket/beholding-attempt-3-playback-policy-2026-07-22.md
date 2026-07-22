# Nemotron 3 adaptive playback simulation

This deterministic replay uses client-side translated-audio arrival timestamps and PCM byte counts from the captured Nemotron 3 traces.

Policy: target 5s, urgent 8s, SLA limit 10s; rates 1.00x / 1.05x / 1.10x. Release hysteresis is 4s / 7s.

| Trace | Chunks | Fixed tail | Adaptive tail | Reduction | Adaptive peak queue | Time >10s | Accelerated audio |
|---|---:|---:|---:|---:|---:|---:|---:|
| Beholding the Love of God | 646 | 64.038s | 14.246s | 77.8% | 28.052s | 633.583s | 92.3% |

Across all 1 replays, listener tail fell from 64.038s to 14.246s (77.8% reduction).

Every translated chunk is retained. The 10-second value is an audience-latency SLA alarm, not a hard cap: if translated audio arrives faster than 1.10x playback can consume it, the queue may still exceed 10 seconds.

`Fixed tail` uses the explicit input-ended event when present, otherwise the exact end of the final source chunk. Historical summaries that used the final chunk's start are accepted only as annotated compatibility evidence. This metric is distinct from whole-file output/input duration drift.
