# Nemotron 3 adaptive playback simulation

This deterministic replay uses client-side translated-audio arrival timestamps and PCM byte counts from the captured Nemotron 3 traces.

Policy: target 5s, urgent 8s, SLA limit 10s; rates 1.00x / 1.05x / 1.10x. Release hysteresis is 4s / 7s.

| Trace | Chunks | Fixed tail | Adaptive tail | Reduction | Adaptive peak queue | Time >10s | Accelerated audio |
|---|---:|---:|---:|---:|---:|---:|---:|
| Spirit and Presence of God | 14,178 | 172.770s | 27.183s | 84.3% | 46.541s | 1535.768s | 93.2% |
| Blessed Self-Forgetfulness | 18,584 | 228.782s | 40.206s | 82.4% | 53.110s | 1905.986s | 92.8% |
| Beholding the Love of God | 13,229 | 70.598s | 16.613s | 76.5% | 23.814s | 867.003s | 83.4% |

Across all three replays, listener tail fell from 472.151s to 84.001s (82.2% reduction).

Every translated chunk is retained. The 10-second value is an audience-latency SLA alarm, not a hard cap: if translated audio arrives faster than 1.10x playback can consume it, the queue may still exceed 10 seconds.

`Fixed tail` is reproduced from the event trace and matches the previously recorded browser queue calculation. It is distinct from whole-file output/input duration drift.
