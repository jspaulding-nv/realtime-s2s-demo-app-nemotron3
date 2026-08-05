# Live tail-freshness shadow frame comparison — 2026-08-05

## Decision

Use 100 ms incremental publication for the next observation-only long-form
shadow gate. Do not promote it as an audible cancellation or general
deployment default yet.

Both five-minute arms completed naturally, read-only attested the same pinned
containers, used the same hash-bound source and pipeline controls, held the
projected queue below 10 seconds, produced only trailing-suffix losses, and
matched the independent Python replay. The 100 ms arm retained more translated
audio, removed less audio, shortened the longest omitted suffix, and remained
stable at approximately five times the frame traffic.

The comparison is not perfectly controlled: live Magpie regeneration produced
2.09% less translated audio in the 100 ms arm. Therefore the full improvement
cannot be attributed to framing alone. The evidence is strong enough to select
the lower-loss profile for further observation, not to make a release claim.

## Matched controls

Both arms used:

- the identical 300-second source WAV with SHA-256
  `78e04698bf76502bc1ae23c5dac8391de0f60a51dc13aae9f042532a24df44d4`;
- Nemotron streaming ASR 1.2.0, Riva Translate 1.5.2, and Magpie TTS 1.7.0;
- 800 ms ASR EOU, word offsets, automatic punctuation, and the same source
  segmentation policy;
- no TTS subsegmentation;
- adaptive playback at 1.00x/1.05x/1.10x;
- a 10-second projected cap and 100 ms cancellation guard;
- rendered-digital PCM capture off; and
- observation-only shadow behavior that changed no audible audio.

Only `STAGED_TTS_INCREMENTAL_FRAME_MS` intentionally differed.

## Results

| Metric | 500 ms | 100 ms | 100 ms change |
|---|---:|---:|---:|
| Natural completion | yes | yes | unchanged |
| Parents | 74 | 74 | unchanged |
| Frames received | 595 | 2,780 | +2,185 |
| Generated translated audio | 279.896 s | 274.044 s | -5.852 s |
| Projected audio retained | 247.844 s | 250.470 s | +2.627 s |
| Projected retention | 88.55% | 91.40% | +2.85 points |
| Projected audio removed | 32.052 s | 23.574 s | -8.478 s |
| Partially truncated parents | 10 | 10 | unchanged |
| Fully dropped parents | 0 | 0 | unchanged |
| Longest removed suffix | 8.143 s | 6.918 s | -1.224 s |
| Peak projected queue | 9.991 s | 9.999 s | both compliant |
| Residual breaches | 0 | 0 | unchanged |
| Actual no-drop queue peak | 24.727 s | 16.491 s | -8.237 s |
| Input end to final zero-queue sample | 27.041 s | 18.032 s | -9.009 s |
| Independent replay | match | match | unchanged |
| Loss shape | suffix only | suffix only | unchanged |

Eight of the ten affected parent IDs overlapped across arms. The affected set
therefore remained broadly stable even though generation duration and arrival
cadence varied.

The 100 ms result also closely reproduced the older retained 100 ms trace:
2,780 versus 2,782 frames and 274.044 versus 274.369 generated seconds. That
historical trace independently held the same cap with suffix-only loss. This
repeatability reduces, but does not eliminate, the live-regeneration
confounder.

## Interpretation

Smaller frames improve the available cut boundary. When the projected queue
barely exceeds the cap, the policy can remove roughly the needed suffix rather
than sacrificing most of a 500 ms frame. Incremental publication can also
expose generated PCM to the browser earlier instead of batching it into larger
bursts.

The current data cannot cleanly separate those effects from Magpie's different
generated duration and response timing between calls. It does establish that
the 100 ms path is operationally stable at five minutes and has no measured
capacity or queue-compliance penalty in this run.

Even the preferred arm projected removing 23.574 seconds, or 8.60% of the
Spanish audio, including one 6.918-second suffix. This remains too destructive
to enable audibly without boundary repair and bilingual review.

## Private evidence

```text
experiment_results/live-tail-shadow-5min-probe-20260805T032142Z/
experiment_results/live-tail-shadow-5min-100ms-probe-20260805T033923Z/
```

Both directories are ignored and owner-private. The aggregate shadow reports
contain no PCM, transcript, translation text, endpoint, filename, or session
identifier. Generated source WAVs and full timing CSVs remain private in those
directories.

## Reproduction

```bash
python3 run_live_tail_shadow_preflight.py \
  --five-minute \
  --incremental-frame-ms 100 \
  --npm /path/to/node-v22.12.0-linux-x64/bin/npm
```

## Next gate

Extend the same production-browser automation across all three complete
long-form samples with an explicit 100 ms profile. Verify natural completion,
container identity, exact replay, cap compliance, suffix-only loss, retention,
and the longest omitted suffix separately for each sample before designing any
audible canary.
