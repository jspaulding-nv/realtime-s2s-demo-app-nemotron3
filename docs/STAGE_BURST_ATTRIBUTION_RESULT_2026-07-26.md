# Stage-burst attribution result — 2026-07-26

## Outcome and claim boundary

The three completed formal headless traces show a strong descriptive
association between bursts of translated audio arriving at the listener and
immediate listener-queue growth. Across the three neutral samples, the
Spearman rank association between parent audio duration and immediate queue
change was 0.999, 0.995, and 0.997.

This is useful attribution evidence, but it is not proof that any one model or
pipeline stage independently caused the backlog. Target length, synthesized
audio duration, stage processing time, queue residence, and publication
cadence can vary together. The pipeline stages also overlap, so their
individual durations must not be added as though they were one serial critical
path.

The narrow conclusion is:

> Immediate queue growth tracked translated-media delivery bursts much more
> closely than it tracked first-frame availability. The current traces do not
> establish independent ASR, NMT, TTS, publisher, transport, or client
> causation.

## Evidence scope and privacy

The analysis covered one complete formal trace for each of three neutral
long-form samples. Each trace passed the existing staged-integrity, frame,
byte, terminal-state, and client-arrival checks before being admitted.

The result uses only relative numeric timing, structural counts, and neutral
sample labels. It contains no transcript or translation text, audio, input
filename or path, endpoint, session identifier, speaker identity, or
organization identity.

All server-stage differences use the server monotonic clock. All client
arrival and playback-schedule differences use the client-relative clock. No
duration subtracts timestamps from different clock domains.

## Measured facts

### Audience availability and scheduled playback

The first translated frame was usually available within several seconds, but
the no-drop playback schedule accumulated substantially more delay later in
the long-form run.

| Metric, seconds | Sample 1 p50 / p95 | Sample 2 p50 / p95 | Sample 3 p50 / p95 |
| --- | ---: | ---: | ---: |
| Source boundary to first browser frame | 2.735 / 5.773 | 2.731 / 5.536 | 2.584 / 4.765 |
| Source boundary to scheduled playback start | — / 74.302 | — / 35.561 | — / 24.090 |

The scheduled-start values are deterministic digital scheduling results under
the registered no-drop adaptive policy. They do not prove physical audibility
or identify a semantic phrase boundary.

### Stage timing distributions

| Metric, seconds | Sample 1 p50 / p95 | Sample 2 p50 / p95 | Sample 3 p50 / p95 |
| --- | ---: | ---: | ---: |
| Source boundary to ASR final, per final | 1.201 / 1.581 | 1.181 / 2.141 | 1.182 / 1.625 |
| Source boundary to latest contributing ASR final, per parent | 1.205 / 1.472 | 1.186 / 1.766 | 1.190 / 1.625 |
| Latest ASR final to segment emission | ≈0.001 / ≈2.058 | ≈0.001 / ≈2.058 | ≈0.001 / ≈2.058 |
| Source boundary to segment emission | 1.270 / 3.384 | 1.259 / 3.423 | 1.278 / 3.373 |
| NMT queue residence | 0.000 / 1.362 | 0.000 / 1.304 | 0.000 / 1.101 |
| NMT processing | 0.376 / 1.137 | 0.384 / 0.968 | 0.336 / 0.840 |
| TTS queue residence | 0.000 / 1.694 | 0.000 / 1.092 | 0.000 / 0.838 |
| TTS start to first output-frame enqueue | 0.225 / 0.267 | 0.227 / 0.267 | 0.223 / 0.260 |
| Full TTS processing | 0.549 / 1.673 | 0.531 / 1.315 | 0.484 / 1.208 |
| Output queue residence | 0.006 / 0.011 | 0.006 / 0.011 | 0.006 / 0.011 |
| Output dequeue to WebSocket send | 0.004 / 0.004 | 0.004 / 0.004 | 0.004 / 0.004 |

The TTS synthesis-to-generated-audio real-time factor was well below 1.0:

| TTS real-time factor | Sample 1 | Sample 2 | Sample 3 |
| --- | ---: | ---: | ---: |
| p50 | 0.197 | 0.205 | 0.206 |
| p95 | 0.313 | 0.333 | 0.311 |

Thus, while TTS was active, it generated audio substantially faster than that
audio's playback duration. That fact does not rule out queueing, unusually
large outputs, or delay in moving responses from the TTS worker into the
publisher.

The second sample recorded three NMT recovery events lasting 282–352 ms. Those
recoveries were shorter than the sample's median successful processing time
and do not explain its long-lived playback backlog. No TTS retry was observed.

### Queue admission pressure

All three bounded work queues reached their four-item capacity during at least
one trace. Producer blocking was concentrated before NMT admission and, to a
lesser extent, before TTS admission.

| Blocked admission | Sample 1 count / total / max | Sample 2 count / total / max | Sample 3 count / total / max |
| --- | ---: | ---: | ---: |
| NMT | 15 / 9.312 s / 6.408 s | 17 / 4.824 s / 1.982 s | 15 / 2.167 s / 0.727 s |
| TTS | 7 / 2.159 s / 0.644 s | 2 / 0.261 s / 0.180 s | 1 / 0.127 s / 0.127 s |
| Output | 3 / 0.044 s / 0.018 s | 5 / 0.055 s / 0.022 s | 0 / 0.000 s / 0.000 s |

Blocked admission occurs before an item enters a queue; queue residence begins
after admission. These intervals are disjoint and should not be conflated.

### Translated-media bursts

Large translated-audio parents were present in every sample.

| Parent audio duration, seconds | Sample 1 | Sample 2 | Sample 3 |
| --- | ---: | ---: | ---: |
| p50 | 2.740 | 2.647 | 2.368 |
| p95 | 9.985 | 7.663 | 6.920 |
| Maximum | 15.650 | 17.044 | 17.554 |

Across the samples, the largest observed parents delivered approximately
13.4–17.6 seconds of playable audio within approximately 1.65–2.72 seconds of
wall time. Their immediate listener-queue increases were approximately
10.1–14.2 seconds.

The largest positive-growth aligned fixed-grid 30-second bin in each sample
was:

| Window result | Sample 1 | Sample 2 | Sample 3 |
| --- | ---: | ---: | ---: |
| Net listener-queue growth | 36.325 s | 21.109 s | 25.842 s |
| Translated media arriving | 72.958 s | 56.053 s | 49.412 s |
| Arrival rate versus real time | 2.432x | 1.868x | 1.647x |

Across the aligned fixed-grid 30-second windows, the descriptive Spearman
association between translated media arriving and net queue growth was 0.849,
0.934, and 0.793. The p95 whole-second rolling translated-media arrival rate
was 1.651x, 1.485x, and 1.395x real time.

At parent level, audio duration versus immediate queue change had Spearman rank
associations of 0.999, 0.995, and 0.997. In contrast,
source-boundary-to-first-frame latency versus immediate queue change had
Spearman associations of 0.158, 0.145, and 0.105.

These associations support investigating output granularity and media duration.
They do not show that parent duration is independent of target text, speaking
rate, model behavior, stage load, or other shared factors.

### Publisher and transport observations

Output-queue blocking and queue residence were small, and server-to-client
frame cadence was closely preserved. The p95 difference between corresponding
server and client inter-frame gaps was below 0.9 ms in all three samples.
This makes WebSocket relay or client receipt an unlikely explanation for the
large observed bursts.

There were, however, rare gaps between a TTS frame being reported as received
and the corresponding output-frame enqueue:

| Unexplained publisher-handoff gap | Sample 1 | Sample 2 | Sample 3 |
| --- | ---: | ---: | ---: |
| Count over 100 ms | 2 | 9 | 2 |
| Count over 1 second | 1 | 7 | 0 |
| Maximum | 5.648 s | 7.054 s | 0.930 s |

These gaps were not accounted for by the recorded output blocked-admission
duration. They are a measurement target, not proof of an event-loop,
cross-thread, or publisher defect.

## Descriptive interpretation

The immediate audience-experience problem in these traces is not simply that a
first translated frame takes several seconds to appear. It is that the no-drop
listener receives translated media faster than it can play that media during
repeated bursts. Once the queue grows, later translated audio can be available
on the client yet remain scheduled tens of seconds behind the source.

The strongest measured relationship is between the amount of translated audio
delivered in a short interval and queue growth during that interval. Active TTS
synthesis was faster than real time, and relay timing was small, so neither
slow active synthesis nor network pacing alone matches the dominant immediate
queue-growth pattern.

That interpretation remains descriptive. The current telemetry cannot
separate all contributors to a large parent, determine why some TTS frames
experience a long handoff gap, or prove which upstream stage should be changed.

Pacing the same no-drop PCM more slowly is not a remedy. It only moves the
backlog from the listener to an upstream server queue while preserving or
increasing source-to-listener delay.

## Limitations

- Each sample has one admitted formal trace; there are no repeated-condition
  confidence intervals.
- The analysis has no transcript, semantic landmark, or acoustic-boundary
  evidence, so it cannot measure the delay of a particular phrase or audience
  reaction.
- ASR source ranges are coarse provenance envelopes rather than word-accurate
  semantic boundaries.
- Parent length, generated-audio duration, processing time, queue residence,
  and publication cadence are confounded.
- Fixed-window membership is based on client frame arrival; a parent can span
  window boundaries.
- Queue capacity counts work items, not seconds of playable audio.
- TTS response-chunk timing is not present, so the analysis cannot distinguish
  service response cadence from worker-to-event-loop publication delay.
- The deterministic client schedule is not a direct measurement of acoustic
  output.
- Correlation values are descriptive and must not be interpreted as causal
  coefficients.

## Recommended next technical step

First add privacy-safe timestamps at every publisher handoff:

1. TTS response received by the worker.
2. Frame publication requested.
3. Event-loop callback begins.
4. Output capacity acquired.
5. Frame enqueued.
6. Frame dequeued and WebSocket send begins.

Also enable privacy-safe TTS response-chunk telemetry using only parent/frame
identity, relative monotonic timing, byte counts, sample counts, and fixed
status codes. Do not capture text, audio, paths, endpoints, or identifiers from
outside the neutral trace schema.

After focused unit and integrity tests, use this sequence:

1. Run a one-minute preflight to verify the new timestamp ordering, identity,
   byte/frame reconciliation, no-drop invariant, and absence of private fields.
2. If the preflight passes, run the shortest five-minute diagnostic canary with
   the existing unsplit, one-TTS-request-per-parent path. This isolates the
   response-chunk and publisher-handoff timings without changing generated
   speech.
3. Compare burst arrival rate, listener-queue p95 and peak,
   source-to-first-frame timing, source-to-scheduled-start timing, blocked
   admission, and the newly isolated publisher-handoff intervals against the
   current baseline.
4. Fix and retest the handoff only if those new timestamps show that it is a
   material contributor. Reject any change that drops or duplicates PCM,
   reorders content, creates unbounded upstream waiting, or merely shifts the
   same backlog to another queue.

Do not repeat the previously rejected 40-, 45-, or 60-character TTS-request
caps as the default next intervention. Those caps made individual parents
smaller but increased total generated audio and worsened the listener queue in
the matched five-minute test.

If the handoff is not material, the next capacity intervention should target
playable duration rather than request count: first measure leading and trailing
TTS silence, then compare a supported native TTS rate control or
pitch-preserving 1.05x/1.10x time-scale arm with the unchanged no-drop
baseline. Translation continuity, Spanish prosody, and intelligibility require
separate native-listener review before promotion.

Only after that short canary passes the integrity and queue gates should a
long-form repeat be considered.
