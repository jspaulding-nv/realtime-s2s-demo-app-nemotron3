# Chrome render-clock shadow diagnostic result

## Decision

The registered 500 ms graph-load profile reproduced one Chrome
`AudioWorklet` frame-clock discontinuity in five 65-second repeats. The
complete generated-PCM trace classified it as a repeated worklet frame label
followed by an immediate catch-up, while the generated source-marker samples
delivered to the shadow worklet remained contiguous.

This result supports a narrow recorder-clock normalization trial. It does not
support weakening the PCM, lifecycle, pacing, queue, or audience-latency
gates, and it does not establish the underlying Chromium lock contention as
the root cause.

## Scope and privacy

The probe:

- used the exact registered recorder worklet;
- created a separate diagnostic worklet with one generated-marker input;
- did not connect translated playback nodes to the diagnostic worklet;
- generated all PCM locally;
- did not start or contact ASR, NMT, or TTS;
- emitted only allowlisted integer counters, fixed status codes, hashes, and
  clock rates; and
- retained four pre-event, one event, and sixteen post-event render quanta.

The diagnostic worklet did not export PCM or text. The strict recorder
continued to fail closed on the first discontinuity.

## Provenance

| Item | Value |
| --- | --- |
| Browser | Chrome `149.0.7827.155` |
| Browser binary SHA-256 | `6aede5b4c357aade7e980470017119f004ceb2cb8f7f8b9d029f85e0f3a60dca` |
| Registered recorder SHA-256 | `8baf6193f097acc3c2663ca91299a19f68b1e7c17deaa586db339a073a7d0a5d` |
| Shadow worklet SHA-256 | `5e8072609462c10d57ff69eca634de7684f5dad9e10b4de7b40898b5066f4076` |
| Reviewed probe script SHA-256 | `4fb4a6d8a9e431fd7fb931480aa1a0ec4c03bf1d4b435d9c1487718657dd0582` |
| AudioContext rate | 16,000 Hz |
| Publication frame | 500 ms |
| Repeats | Five at 65 seconds each |
| Synthetic translated sources | 60 per repeat |

Reproduce without Riva:

```bash
python3 probe_rendered_digital_graph_load.py \
  --frame-ms 500 \
  --repeats 5 \
  --probe-seconds 65 \
  --burst-at-seconds 6 \
  --trace-rewind
```

A nonzero exit is expected when the strict recorder reproduces the
discontinuity. An accepted trace must have diagnostic status `traced`, no
diagnostic failure codes, and a complete 21-entry trace. An incomplete or
internally inconsistent shadow trace also exits nonzero.

The event is stochastic: a later five-of-five clean run with exit zero would
not contradict this one-of-five observation. The reviewed script hash above
includes two post-run hardenings found during independent review: source
length alignment for every allowed duration and explicit rejection of a raw
trace longer than 21 entries. For the integer 65-second run, the generated
source length, browser graph, shadow-worklet bytes, retained entries, and
classification logic used by this result are unchanged.

## Result

Repeat 1 reported an elapsed-client value of approximately 28.619 seconds at
the error delivery. Repeats 2–5 completed with no capture error.

| Repeat | Strict recorder | Shadow diagnostic |
| --- | --- | --- |
| 1 | `noncontiguous_render_quantum`, delta `-128` | `traced` |
| 2 | No error | `not_reproduced` |
| 3 | No error | `not_reproduced` |
| 4 | No error | `not_reproduced` |
| 5 | No error | `not_reproduced` |

The strict recorder and shadow worklet both reported expected frame `458752`
and observed frame `458624`. Selected entries from the complete trace show the
classification:

| Callback | Role | Logical frame | Observed `currentFrame` | Clock step | Decoded source frame | Source-sample step |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 3580 | Before | 458624 | 458624 | 128 | 454144 | 128 |
| 3581 | Event | 458752 | 458624 | 0 | 454272 | 128 |
| 3582 | Catch-up | 458880 | 458880 | 256 | 454400 | 128 |
| 3597 | Final retained | 460800 | 460800 | 128 | 456320 | 128 |

Both worklet `currentFrame` and the frame derived from worklet `currentTime`
repeated at callback 3581. They caught up on the next callback and then
advanced normally through all sixteen retained post-event quanta. Every
decoded generated-source marker advanced by exactly 128 frames across the
entire trace. No generated sample duplicate or drop was observed.

The main-thread context was `running` and had reached frame `458880` when the
asynchronous error message was delivered. That is only a message-delivery
observation; it does not establish the main-thread frame at the exact render
quantum.

## Source-backed mechanism hypothesis

The root cause remains an inference. In the exact Chrome tag used here, the
real-time destination
[advances its destination frame counter before the worklet-global
update](https://chromium.googlesource.com/chromium/src/+/refs/tags/149.0.7827.155/third_party/blink/renderer/modules/webaudio/realtime_audio_destination_handler.cc#273).
The
[worklet-global update uses the graph lock through a non-blocking
`TryLock`](https://chromium.googlesource.com/chromium/src/+/refs/tags/149.0.7827.155/third_party/blink/renderer/modules/webaudio/base_audio_context.cc#977).
Both
[`AudioNode.connect()`](https://chromium.googlesource.com/chromium/src/+/refs/tags/149.0.7827.155/third_party/blink/renderer/modules/webaudio/audio_node.cc#150)
and
[scheduled-source start handling](https://chromium.googlesource.com/chromium/src/+/refs/tags/149.0.7827.155/third_party/blink/renderer/modules/webaudio/base_audio_context.cc#778)
use that graph lock.

This makes a skipped worklet-global update during graph mutation consistent
with the observed repeated label and next-quantum catch-up. The trace does not
prove that contention occurred at the event, so an upstream report should
present this as the leading hypothesis, not as a confirmed cause.

If the repeated worklet label while the context is running is confirmed
upstream, it appears inconsistent with the Web Audio requirements that
[`currentTime` increase monotonically while running](https://webaudio.github.io/web-audio-api/#dom-baseaudiocontext-currenttime),
that worklet
[`currentFrame` reflect the context's current-frame
slot](https://webaudio.github.io/web-audio-api/#dom-audioworkletglobalscope-currentframe),
and that rendering
[increment the current frame by one render
quantum](https://webaudio.github.io/web-audio-api/#rendering-a-graph).

## Recommended implementation trial

Keep the exact registered worklet unchanged until the diagnostic change is
reviewed. In the next focused change:

1. Treat the callback ordinal as the capture PCM position.
2. Allow only one pending clock-label pattern: a zero-frame worklet-clock step
   followed immediately by a two-quantum step that restores the logical
   frame.
3. Preserve all PCM blocks during that pair and record a fixed, integer-only
   normalization counter.
4. Fail on a missing immediate catch-up, a second event, any other step or
   offset, an invalid lifecycle, or an out-of-range wall-clock pacing result.
5. Add worklet-harness tests for the accepted pair and every rejected
   neighbor, then repeat this generated-marker diagnostic before attempting
   the formal live gate.

This is narrower than replacing the playback graph. A persistent playback
worklet remains a later A/B candidate if graph-mutation instability continues,
but the current trace does not show a duplicate or drop in the generated
source-marker samples delivered to the shadow worklet.

This recorder fix is an evidence-capture prerequisite only. It does not make
live translation faster or resolve the separate audience-experience question;
semantic source-to-translated-audio delay still requires the bounded-queue and
same-clock audience-latency gates.
