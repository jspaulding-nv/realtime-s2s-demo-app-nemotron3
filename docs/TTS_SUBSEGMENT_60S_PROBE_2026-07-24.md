# Post-NMT TTS subsegmentation: 60-second live probe

## Status and scope

This was a deliberately non-formal probe from the implementation worktree. It
was used to catch live contract failures before committing and running the
five-minute evidence matrix. It must not be treated as a promotion result.

The runner created one shared 60-second, 16 kHz mono PCM prefix and reused its
exact bytes for all four policies. It inspected the unique running ASR, NMT,
and TTS containers by their configured HTTP ports, verified the pinned image
tags and immutable repository digests, and passed those digests into FastAPI.

All four arms:

- completed without a model retry, terminal error, cleanup error, or dropped
  playback chunk;
- passed schema-aware child lifecycle and frame-by-frame PCM integrity;
- used the same 200 input chunks and 16 upstream NMT parents; and
- matched on backend/model provenance, ASR-final structure, parent
  segmentation, NMT parent structure, and playback policy.

The comparison contains no transcript, audio, source path, endpoint, or
session identifier.

## Result

| TTS cap | Parent / child calls | Child duration p95 / max | Output/input | First audio | Captured tail | Adaptive queue p95 | Adaptive tail | Time above 10 s |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Disabled | 16 / 16 | 11.378 s / 11.378 s | 0.8677 | 15.723 s | 7.709 s | 10.344 s | 4.841 s | 6.40% |
| 40 | 16 / 27 | 3.297 s / 3.901 s | 0.9242 | 15.660 s | 11.038 s | 12.986 s | 7.019 s | 30.99% |
| 45 | 16 / 26 | 3.483 s / 3.529 s | 0.9187 | 15.550 s | 10.608 s | 12.541 s | 6.664 s | 27.05% |
| 60 | 16 / 21 | 4.040 s / 4.133 s | 0.9025 | 15.495 s | 9.578 s | 11.123 s | 5.924 s | 14.99% |

Splitting achieved its narrow mechanical goal: p95 child duration fell by
64.5–71.0%, and maximum delivery bursts fell from 11.378 seconds to at most
4.133 seconds. It did not improve the measured listener experience:

- 40 characters increased TTS calls by 68.8%, generated-duration ratio by
  6.5% relative to control, adaptive queue p95 by 25.5%, and adaptive tail by
  45.0%.
- 45 characters increased calls by 62.5%, generated-duration ratio by 5.9%,
  adaptive queue p95 by 21.2%, and adaptive tail by 37.7%.
- 60 characters was the least harmful split policy, but still increased calls
  by 31.3%, generated-duration ratio by 4.0%, adaptive queue p95 by 7.5%, and
  adaptive tail by 22.4%.

First audio remained approximately 15.5–15.7 seconds in every arm. The largest
observed change was only 0.23 seconds, so post-NMT request splitting did not
materially reduce the initial delay a live listener would notice.

## Interpretation

The result is consistent with per-request TTS overhead. More, shorter requests
produce smaller PCM bursts but also more total audio, which grows the listener
queue faster than the existing 1.05x/1.10x adaptive playback policy can remove
it. The short probe therefore rejects automatic promotion of 40 or 45
characters and does not yet support promoting 60 characters.

This does not invalidate punctuation-aware boundaries as a language-quality
tool. It shows that issuing each boundary as a separate TTS request is not, by
itself, a latency solution for this runtime.

## Next gate

Run the committed five-minute matched matrix to determine whether the same
tradeoff persists as queue pressure accumulates. If it does:

1. keep `STAGED_TTS_SUBSEGMENT_MAX_CHARS=0`;
2. investigate streaming smaller PCM frames from one parent TTS request so
   delivery improves without multiplying request overhead;
3. measure whether per-request leading/trailing silence explains the added
   duration before considering carefully bounded silence trimming;
4. retain a bounded listener queue target of approximately 5–10 seconds and
   evaluate 1.05x–1.10x playback exposure with native-language review; and
5. require synchronized source-event-to-audible-output markers before making
   a claim about live joke or audience-reaction timing.
