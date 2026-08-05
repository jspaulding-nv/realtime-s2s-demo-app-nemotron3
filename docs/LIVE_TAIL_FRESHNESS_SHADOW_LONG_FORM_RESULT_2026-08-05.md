# Complete long-form live tail-shadow result — 2026-08-05

## Decision

The 100 ms observation-only shadow passed its mechanical contract on all three
complete registered long-form samples. Every run completed naturally, the
independent Python replay matched the browser decisions, the projected queue
remained below 10 seconds, residual breaches were zero, and the single-tail
loss-shape contract passed.

Do **not** promote the projected cancellation policy to audible behavior. The
three shadows would remove 731.300 seconds of translated speech in aggregate,
or 10.97% of generated Spanish audio. They would truncate 441 translated
parents, fully remove one parent, and remove one trailing suffix lasting 13.382
seconds. A mechanical freshness bound is therefore possible, but the current
policy reaches it by projecting too much unreviewed content loss.

The unchanged adaptive no-drop browser queue still peaked between 36.252 and
70.899 seconds. The existing 1.00x/1.05x/1.10x playback controller is not enough
to hold the desired 5–10 second audience range on these complete samples.

## Fixed runtime

- Repository commit: `45c4169dab0e4acb68c03c47baf5c81cdbd7820f`;
- Nemotron streaming ASR `1.2.0` with the recorded image digest;
- Riva Translate `1.5.2` with the recorded image digest;
- Magpie multilingual TTS `1.7.0` with the recorded image digest;
- 800 ms ASR EOU, word offsets, and automatic punctuation;
- staged ASR, NMT, and TTS queues;
- schema-3 TTS publication in 100 ms frames;
- adaptive playback at 1.00x/1.05x/1.10x;
- a 10-second projected queue cap and 100 ms cancellation guard;
- rendered-digital PCM capture disabled; and
- observation-only behavior: live Spanish audio was unchanged.

The runner read-only attested all three live container identities for every
sample. The source MP3 identities matched `test_audio/SHA256SUMS`, and each
private decoded PCM fixture received its own SHA-256 identity.

## Per-sample result

| Metric | Sample 01 | Sample 02 | Sample 03 |
|---|---:|---:|---:|
| Source duration | 1,908.432 s | 2,427.011 s | 1,888.105 s |
| Generated Spanish audio | 2,097.291 s | 2,631.032 s | 1,939.769 s |
| Generated/source expansion | 9.896% | 8.406% | 2.736% |
| Frames / parents | 21,263 / 580 | 26,730 / 805 | 19,716 / 643 |
| Actual adaptive no-drop queue peak | 70.899 s | 53.117 s | 36.252 s |
| Projected retained audio | 85.57% | 89.36% | 92.34% |
| Projected removed audio | 302.649 s | 280.045 s | 148.606 s |
| Truncated / fully removed parents | 166 / 1 | 176 / 0 | 99 / 0 |
| Longest removed suffix | 12.236 s | 9.103 s | 13.382 s |
| Peak projected queue | 10.000 s | 10.000 s | 10.000 s |
| Residual cap breaches | 0 | 0 | 0 |
| Independent replay | match | match | match |
| Single-tail contract | pass | pass | pass |

## Aggregate result

| Metric | Aggregate |
|---|---:|
| Source audio | 6,223.547 s / 103.726 min |
| Generated Spanish audio | 6,668.092 s / 111.135 min |
| Generated/source ratio | 1.07143 |
| Spanish expansion | 7.143% |
| Frames / parents | 67,709 / 2,028 |
| Projected retained audio | 5,936.792 s / 89.03% |
| Projected removed audio | 731.300 s / 10.97% |
| Truncated / fully removed parents | 441 / 1 |
| Worst removed suffix | 13.382 s |
| Residual cap breaches | 0 |

## Interpretation

The new ASR and staged server changes provide a reliable, reproducible pipeline,
but they do not eliminate the listener-queue problem. Spanish duration expansion
matches the expected long-form mechanism, while burst delivery and upstream
phrase latency produce much larger instantaneous queues than the 7.143% average
expansion alone would predict.

The result also separates two claims:

1. A causal 10-second scheduled-audio cap can be enforced mechanically on
   already-delivered browser audio.
2. Enforcing it safely for listeners has not been demonstrated.

The second claim fails the current engineering gate because an audience could
lose complete ideas, including one entire translated parent and multi-second
parent suffixes. No mechanical invariant establishes semantic safety.

## Recommended next technical gate

Keep audible cancellation disabled. Reuse these exact arrival traces for a fast
offline counterfactual that measures constant and adaptive pitch-preserving
time-scale policies before another real-time run. Determine the minimum playback
rate and threshold policy needed to hold queue p95 and peak near 10 seconds with
zero content loss. Include at least 1.10x, 1.15x, 1.20x, and a bounded ramp policy.

If no listener-tolerable rate holds the bound, the architecture needs an
explicit product choice among translated-content compression, summarization,
source-speaker pacing, or a larger audience-delay SLA. Tail cancellation should
not become the implicit answer.

Any promising time-scale arm must then use pitch-preserving processing and pass
native-Spanish review for intelligibility, naturalness, boundary continuity,
and listening fatigue.

## Private evidence

The complete ignored owner-private batch directory is:

```text
experiment_results/live-tail-shadow-long-form-100ms-20260805T150331Z-45c4169/
```

It contains the checkpoint manifest plus one immutable attempt directory per
sample. Each attempt retains the decoded source WAV, numeric shadow JSON, timing
CSV, application logs, Docker attestation, and independent analyzer reports.
Do not commit or externally publish this directory without a separate privacy
review.

## Claim boundaries

This result does not measure exact English-event to Spanish-event semantic delay,
prove translation accuracy, approve 1.10x audio quality, or make projected
omissions safe. The 10-second value is a browser scheduled-audio freshness cap,
not a complete room-audience latency guarantee.
