# Live tail-freshness shadow five-minute result — 2026-08-05

## Decision

The observation-only live capacity gate passed. On a hash-identical
five-minute source, the browser's causal single-tail shadow held the projected
scheduled-audio queue below 10 seconds, produced zero residual breaches, and
removed only continuous trailing parent suffixes. The independent Python
replay matched every live decision.

Audible Spanish playback remained unchanged. Its real no-drop scheduled queue
still reached 24.727 seconds and took approximately 27.041 seconds after input
ended to reach the final zero-queue sample. The shadow demonstrates a
mechanically viable freshness bound, not an approved audible policy.

This was a non-formal engineering probe from a dirty working tree. The runner
did hash-check the exact source and read-only attest all three live container
images before capture.

## Exact runtime

- Source: exact 300-second, 16 kHz mono prefix registered by SHA-256
  `78e04698bf76502bc1ae23c5dac8391de0f60a51dc13aae9f042532a24df44d4`;
- Nemotron streaming ASR 1.2.0;
- Riva Translate 1.5.2;
- Magpie multilingual TTS 1.7.0;
- 800 ms ASR EOU, word offsets, and automatic punctuation;
- staged schema-3 incremental publication in 500 ms frames;
- adaptive playback at 1.00x/1.05x/1.10x;
- 10-second projected queue cap and 100 ms cancellation guard; and
- rendered-digital PCM capture disabled.

The exact source was regenerated from the tracked long-form fixture by the
runner and matched the retained historical prefix byte-for-byte.

## Live result

| Metric | Result |
|---|---:|
| Natural completion | yes |
| Independent replay | exact match |
| Translated parents | 74 |
| Schema-3 frames received | 595 |
| Generated translated audio | 279.896 s |
| Projected audio retained | 247.844 s / 88.55% |
| Projected audio removed | 32.052 s / 11.45% |
| Parents partially truncated | 10 |
| Parents fully dropped | 0 |
| Longest removed suffix | 8.143 s |
| Peak projected queue after truncation | 9.991 s |
| Residual cap breaches | 0 |
| Hard cap achieved | yes |
| Single-tail invariant | pass |

All ten affected parents retained one continuous prefix and lost only the
remaining suffix. There were no prefix cuts, internal gaps, fragmented gaps,
or fully removed parents. Fourteen later frames from already-truncated parents
were causally suppressed in the projection through their completion markers.

## Live versus retained offline prediction

The source hash, three model versions, parent count, playback policy, cap, and
guard match. Two material differences remain: the retained historical trace
used 100 ms publication frames, while this current profile uses 500 ms frames;
and live TTS regeneration produced 2.01% more audio.

| Metric | Retained offline trace | New live shadow |
|---|---:|---:|
| Publication frame target | 100 ms | 500 ms |
| Parents | 74 | 74 |
| Frames | 2,782 | 595 |
| Generated translated audio | 274.369 s | 279.896 s |
| Audio retained | 90.49% | 88.55% |
| Audio removed | 26.088 s | 32.052 s |
| Affected parents | 9 | 10 |
| Longest suffix removed | 7.289 s | 8.143 s |
| Peak projected queue | 9.999 s | 9.991 s |
| Residual breaches | 0 | 0 |
| Loss shape | suffix only | suffix only |

Seven of the nine historically affected parent IDs also appear in the new
live affected set. That overlap, the identical parent count, and the exact
replay support the policy's causal stability. The retention difference should
not be called a regression because the TTS output and frame granularity are
not matched.

The coarser 500 ms boundary can require removing more speech than a 100 ms
boundary when only part of the newest frame is needed to restore the cap. A
matched 100 ms/500 ms live comparison is therefore the lowest-cost next gate
before spending multiple hours on all three long-form samples.

## Private evidence

The ignored owner-private directory is:

```text
experiment_results/live-tail-shadow-5min-probe-20260805T032142Z/
```

The shadow and aggregate analysis contain no PCM, transcript, translation
text, source filename, endpoint, or session identifier. The directory also
contains the private generated source WAV and timing CSV and must remain
ignored.

## Reproduction

With the pinned Riva services already healthy and Node.js 22.12.0 or newer:

```bash
python3 run_live_tail_shadow_preflight.py \
  --five-minute \
  --npm /path/to/node-v22.12.0-linux-x64/bin/npm
```

## Completed follow-on

The matched 100 ms arm passed and is documented in the
[frame comparison](LIVE_TAIL_FRESHNESS_SHADOW_FRAME_COMPARISON_2026-08-05.md).
It retained 91.40% of generated audio and shortened the worst projected suffix
to 6.918 seconds while remaining operationally stable. Use 100 ms explicitly
for the next observation-only three-sample gate.

No mechanical result makes an 8.143-second omitted suffix semantically safe.
Audible cancellation still requires a separate default-off canary with fades,
boundary restart, and bilingual review.
