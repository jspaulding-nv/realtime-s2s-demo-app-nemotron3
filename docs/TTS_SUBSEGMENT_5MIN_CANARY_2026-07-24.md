# Post-NMT TTS subsegmentation: five-minute matched canary

## Decision

Keep post-NMT TTS request splitting disabled:

```dotenv
STAGED_TTS_SUBSEGMENT_MAX_CHARS=0
```

The formal five-minute matrix showed that 40-, 45-, and 60-character caps all
reduced individual TTS delivery-burst duration, but every enabled policy
increased total synthesized audio and worsened the listener queue and tail.
The 60-character cap was least harmful, but it still failed the
control-relative promotion gate.

## Evidence contract

The matrix ran from clean commit `4367931` and used one shared 300-second,
16 kHz mono PCM prefix for every arm. The runner:

- found the unique running ASR, NMT, and TTS containers by configured HTTP
  port;
- verified the pinned image tags and immutable repository digests;
- verified FastAPI reported those same images and digests for every arm;
- sent the same 1,000 real-time input chunks in every capture; and
- kept all raw audio, logs, traces, paths, endpoints, and session identifiers
  under an ignored evidence directory.

All arms completed without a model retry, terminal error, cleanup error, or
dropped playback chunk. Schema-aware lifecycle and frame-by-frame PCM
integrity passed. The matched-design gate also proved identical backend/model
configuration, ASR-final structure, 74 upstream NMT parents, parent
segmentation, NMT parent structure, and playback policy.

## Results

| TTS cap | Parent / child calls | Child duration p95 / max | Output/input | First audio | Captured 1x tail | Adaptive queue p95 | Adaptive peak | Adaptive tail | Time above 10 s |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Disabled | 74 / 74 | 12.771 s / 15.883 s | 0.9921 | 15.488 s | 39.473 s | 19.339 s | 27.001 s | 30.510 s | 23.11% |
| 40 | 74 / 159 | 2.972 s / 4.180 s | 1.0466 | 15.479 s | 45.357 s | 24.251 s | 32.225 s | 36.546 s | 29.56% |
| 45 | 74 / 144 | 3.158 s / 3.947 s | 1.0349 | 15.549 s | 45.952 s | 25.056 s | 32.377 s | 36.729 s | 32.36% |
| 60 | 74 / 117 | 4.087 s / 4.458 s | 1.0079 | 15.474 s | 41.450 s | 21.660 s | 28.877 s | 32.969 s | 24.36% |

Relative to the disabled control:

| TTS cap | Call amplification | Child p95 reduction | Child max reduction | Output-ratio increase | Adaptive queue-p95 increase | Adaptive-tail increase |
|---:|---:|---:|---:|---:|---:|---:|
| 40 | +114.9% | 76.7% | 73.7% | +5.5% | +25.4% | +19.8% |
| 45 | +94.6% | 75.3% | 75.1% | +4.3% | +29.6% | +20.4% |
| 60 | +58.1% | 68.0% | 71.9% | +1.6% | +12.0% | +8.1% |

The first-audio difference across all four arms was only 0.075 seconds. Request
splitting therefore did not materially change startup latency.

## Audience interpretation

The audience-experience concern remains real. Even the unsplit control took
about 15.5 seconds to produce first audio. Its simulated no-drop adaptive
listener still had:

- a 19.3-second time-weighted queue p95;
- a 27.0-second peak queue;
- 23.1% of the playback window above the intended 10-second ceiling; and
- a 30.5-second listener tail after source input ended.

The backend service tail was only 3.5 seconds for the control, but the listener
tail was much larger because completed audio arrived in bursts and still had
to be played. That distinction matters for a live joke: a backend can be
technically near completion while the audience remains many seconds behind the
moment that caused the room to react.

Splitting made the bursts smaller, but the additional TTS requests generated
enough extra audio to make the queue worse. The best split policy, 60
characters, still produced a 21.7-second adaptive queue p95 and a 33.0-second
adaptive tail.

These values are workload-level timing evidence, not a synchronized joke-event
measurement. A production claim still requires source-event and
translated-audible markers plus native-language review.

## Recommended next technical step

The highest-value next experiment is to preserve one TTS request per complete
NMT parent while streaming smaller PCM frames from that request as they arrive.
That targets the 12–16 second delivery bursts without multiplying per-request
overhead or reducing NMT context.

The implementation should use explicit streaming failure semantics: retry only
before any PCM is published; after publication, fail the stream without
replaying already-heard audio. A small unpublished prefix buffer can retain
limited retry protection while bounding startup delay.

In parallel:

1. capture child PCM in an ignored diagnostic run and measure leading/trailing
   silence per TTS request to determine whether padding explains the added
   duration;
2. keep a listener queue target of approximately 5–10 seconds, but recognize
   that the current no-drop 1.05x/1.10x policy did not hold that bound;
3. test synthesized prosody or playback speed around 1.05x–1.10x with
   native-language quality review;
4. explicitly decide the overload policy if production stays behind: stronger
   speed-up, content-aware compression, or a controlled skip-to-live action;
   and
5. measure synchronized source-event-to-audible-output delay before evaluating
   joke and audience-reaction timing.
